from __future__ import annotations
from typing import Callable, List, Tuple, Any, Dict, cast, Union, Optional, Set
import functools, itertools
from tinygrad.helpers import DEBUG, DType, merge_dicts, GlobalCounters, getenv, colored, partition
from tinygrad.ops import RawBuffer, Device, ASTRunner
from tinygrad.tensor import Tensor
from tinygrad.shape.shapetracker import ShapeTracker
from tinygrad.shape.symbolic import Variable, NumNode, sym_infer
from dataclasses import dataclass
from weakref import ref, WeakKeyDictionary

JIT_SUPPORTED_DEVICE = ["GPU", "CLANG", "METAL", "CUDA", "HIP", "WEBGPU", "LLVM"]

@dataclass(frozen=True)
class JitItem:
  prg: ASTRunner
  rawbufs: List[Optional[RawBuffer]]

class BatchExecutor:
  def __init__(self, jit_cache: List[JitItem], input_rawbuffers: Dict[Union[int, str], RawBuffer], var_vals: Dict[Variable, int]):
    self.jit_cache: List[JitItem] = jit_cache
    self.input_replace: Dict[Tuple[int, int], Union[int, str]] = {}
    self.op_estimate, self.mem_estimate = NumNode(0), NumNode(0)
    for j,ji in enumerate(jit_cache):
      if isinstance(ji.prg, ASTRunner):  # TODO: this is just for world and needs to be refactored
        self.op_estimate += ji.prg.op_estimate
        self.mem_estimate += ji.prg.mem_estimate
      for i,a in enumerate(ji.rawbufs):
        if a in [v for v in input_rawbuffers.values()]:
          self.input_replace[(j,i)] = [k for k,v in input_rawbuffers.items() if v == a][0]
    assert set(self.input_replace.values()) == set(input_rawbuffers.keys()), "some input tensors not found"
    self.clear_jit_inputs()

  def __call__(self, input_rawbuffers: Dict[Union[int, str], RawBuffer], var_vals: Dict[Variable, int], wait=False):
    for (j,i),input_name in self.input_replace.items(): self.jit_cache[j].rawbufs[i] = input_rawbuffers[input_name]
    for ji in self.jit_cache: ji.prg(cast(List[RawBuffer], ji.rawbufs), {v:var_vals[v] for v in getattr(ji.prg,"vars",[])}, jit=True)
    self.clear_jit_inputs()

  def update_stats(self, var_vals: Dict[Variable, int], et: Optional[float]):
    # TODO: this is mostly copied from ASTRunner
    op_estimate = sym_infer(self.op_estimate, var_vals)
    mem_estimate = sym_infer(self.mem_estimate, var_vals)
    if DEBUG >= 2:
      print(f"{colored(f'*** {GlobalCounters.kernel_count:4d}', 'CYAN')}    kernels:{len(self.jit_cache):4d}  inputs:{len(self.input_replace):3d}   {' '.join([f'{k.expr}={v}' for k,v in var_vals.items()])[:50]:50s} OPs {int(op_estimate/1e6):6d}M/{GlobalCounters.global_ops/1e9:7.2f}G  mem {GlobalCounters.mem_used/1e9:5.2f} GB " +
            (str() if et is None else f"tm {et*1e6:9.2f}us/{GlobalCounters.time_sum_s*1e3:9.2f}ms ({op_estimate/((et or 1e-20)*1e9):8.2f} GFLOPS, {mem_estimate/((et or 1e-20)*1e9):7.2f} GB/s)"))
    GlobalCounters.kernel_count += len(self.jit_cache)
    GlobalCounters.global_ops += sym_infer(self.op_estimate, var_vals)
    GlobalCounters.global_mem += sym_infer(self.mem_estimate, var_vals)
    if et is not None: GlobalCounters.time_sum_s += et

  def clear_jit_inputs(self):
    for (j,i) in self.input_replace.keys(): self.jit_cache[j].rawbufs[i] = None

class TinyJit:
  def __init__(self, fxn:Callable):
    self.fxn: Callable = fxn
    self.jit_fxn: Optional[BatchExecutor] = None
    self.cnt: int = 0
    self.ret: Any = None
    self.expected_vals: Optional[Tuple[Variable, ...]] = None
    self.expected_sts_dtype: Optional[Tuple[Tuple[ShapeTracker, DType], ...]] = None

  @property
  def jit_cache(self) -> List[JitItem]: return self.jit_fxn.jit_cache if self.jit_fxn else []
  @property
  def input_replace(self) -> Dict[Tuple[int, int], Union[int, str]]: return self.jit_fxn.input_replace if self.jit_fxn else {}

  # add support for instance methods
  def __get__(self, obj, objtype): return functools.partial(self.__call__, obj)

  def __call__(self, *args, **kwargs) -> Any:
    if Device.DEFAULT.split(":")[0] not in JIT_SUPPORTED_DEVICE: return self.fxn(*args, **kwargs)  # only jit on supported device

    # all inputs are realized
    input_tensors: Dict[Union[int, str], Tensor] = {cast(Union[int, str], k):v.realize() for k,v in itertools.chain(enumerate(args), kwargs.items()) if v.__class__ is Tensor}
    expected_sts_dtype = tuple([(v.lazydata.st.unbind(), v.dtype) for v in input_tensors.values()])

    # get rawbuffers
    input_rawbuffers: Dict[Union[int, str], RawBuffer] = {k:cast(RawBuffer, v.lazydata.realized) for k,v in input_tensors.items()}
    assert len(input_rawbuffers) != 0, "no inputs to JIT"
    assert len(set(input_rawbuffers.values())) == len(input_rawbuffers), "duplicate inputs to JIT"

    # get variables: they can either be in Tensors or passed in as arguments, and all must be bound. these are all global
    var_vals: Dict[Variable, int] = merge_dicts([arg.lazydata.st.var_vals for arg in input_tensors.values()] + [dict(x.unbind() for x in itertools.chain(args, kwargs.values()) if isinstance(x, Variable))])
    expected_vals = tuple(var_vals.keys())

    if self.cnt >= 2:
      assert self.expected_vals == expected_vals, "mismatch of var_vals"
      assert self.expected_sts_dtype == expected_sts_dtype, "mismatch of sts"
      assert self.jit_fxn, "didn't get jitted?"
      self.jit_fxn(input_rawbuffers, var_vals, DEBUG>=2)
    elif self.cnt == 1:
      self.expected_vals, self.expected_sts_dtype = expected_vals, expected_sts_dtype

      CacheCollector.start(var_vals)
      self.ret = self.fxn(*args, **kwargs)
      jit_cache = CacheCollector.finish()
      assert len(jit_cache) != 0, "didn't JIT anything!"
      if DEBUG >= 1: print(f"JIT captured {len(jit_cache)} kernels with {len(input_rawbuffers)} inputs")

      # get kernels that depend on the inputs
      depends: Set[Optional[RawBuffer]] = set(input_rawbuffers.values())
      for ji in jit_cache:
        if any(b in depends for b in ji.rawbufs[1:]):
          depends.add(ji.rawbufs[0])
      jit_cache, jit_cache_independent = partition(jit_cache, lambda ji: ji.rawbufs[0] in depends)

      # run the independent here and once
      for ji in jit_cache_independent: ji.prg(cast(List[RawBuffer], ji.rawbufs), {v:var_vals[v] for v in getattr(ji.prg,"vars",[])}, jit=True)

      # batch exec for the depends
      alt_batch_exec = Device[Device.DEFAULT].batch_executor
      self.jit_fxn = (BatchExecutor if alt_batch_exec is None or getenv("JIT") == 2 else alt_batch_exec)(jit_cache, input_rawbuffers, var_vals)
    elif self.cnt == 0:
      self.ret = self.fxn(*args, **kwargs)

    self.cnt += 1
    return self.ret

class PlaceHolder:
  def __init__(self, buf:RawBuffer): self.size, self.dtype, self._device, self.ref, self.buftype, self.bufid = buf.size, buf.dtype, getattr(buf, '_device', None), ref(buf), type(buf), id(buf._buf)
  def to_tuple(self): return (self.size, self.dtype, self._device, self.buftype, self.bufid)
  def __hash__(self): return hash(self.to_tuple())
  def __eq__(self, x): return isinstance(x, PlaceHolder) and self.to_tuple() == x.to_tuple()
  def alloc_if_needed(self, buffer_cache: Dict[PlaceHolder, RawBuffer]) -> RawBuffer:
    ret = self.ref()
    if ret: return ret
    if self not in buffer_cache: buffer_cache[self] = self.buftype(self.size, self.dtype, **({'device':self._device} if self._device is not None else dict()))
    return buffer_cache[self]

class _CacheCollector:
  def __init__(self):
    self.cache: Optional[List[Tuple[ASTRunner, List[Union[RawBuffer, PlaceHolder]]]]] = None

  def start(self, var_vals:Optional[Dict[Variable, int]]=None):
    self.cache = []
    self.placeholders: WeakKeyDictionary[RawBuffer, PlaceHolder] = WeakKeyDictionary()
    self.var_vals = var_vals if var_vals is not None else {}

  def add(self, prg, rawbufs, var_vals):
    if self.cache is None: return
    for k,v in var_vals.items(): assert k in self.var_vals and self.var_vals[k] == v, f"var_vals {k} mismatch {v} != {self.var_vals.get(k)}"
    self.placeholders[rawbufs[0]] = PlaceHolder(rawbufs[0])
    self.cache.append((prg, [self.placeholders.get(x, x) if isinstance(x, RawBuffer) else x for x in rawbufs]))

  def finish(self) -> List[JitItem]:
    if self.cache is None: return []
    buffer_cache: Dict[PlaceHolder, RawBuffer] = {}
    saved_cache, self.cache = self.cache, None
    return [JitItem(prg, [x.alloc_if_needed(buffer_cache) if isinstance(x, PlaceHolder) else x for x in pl]) for prg, pl in saved_cache]
CacheCollector = _CacheCollector()
