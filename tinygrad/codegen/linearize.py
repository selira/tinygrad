from __future__ import annotations
from typing import List, Dict, Tuple, DefaultDict
import functools, heapq
from collections import defaultdict
from tinygrad.ops import type_verify, UOp, Ops
from tinygrad.dtype import dtypes
from tinygrad.helpers import dedup, flatten, DEBUG

from tinygrad.ops import PatternMatcher, UPat, graph_rewrite
class BasicBlock:
  def __init__(self, rngs, lst):
    self.rngs = tuple(rngs)
    self.lst = tuple(lst)
  def __hash__(self): return hash((self.rngs, self.lst))
  def __eq__(self, x): return self.rngs == x.rngs and self.lst == x.lst
  def __repr__(self):
    return f"{[y.arg[0] if y.op is Ops.RANGE else f'IF{id(y)}' for y in self.rngs]} {len(self.lst)}" + "\n" + '\n'.join([str(x.op) for x in self.lst])
  @functools.cached_property
  def lst_tuplize(self): return tuple(y.tuplize for y in self.lst)
  @functools.cached_property
  def rngs_tuplize(self): return tuple(y.tuplize for y in self.rngs)
  def __lt__(self, x:BasicBlock):
    if self.rngs == x.rngs: return False # self.lst_tuplize < x.lst_tuplize
    return self.rngs_tuplize < x.rngs_tuplize
  def add(self, x):
    if len(x) == 0: return self
    return BasicBlock(self.rngs, tuple(x)+self.lst)

@functools.lru_cache(None)
def get_ranges_in_parents(x:UOp) -> Tuple[UOp, ...]:
  ret: List[Tuple[UOp, ...]] = []
  for u in x.src:
    if u.op in {Ops.RANGE, Ops.IF}: ret.append((u,))
    # don't flow through assign and store
    if u.op is Ops.STORE: continue
    if u.op is Ops.ASSIGN:
      assert u.src[0].op is Ops.DEFINE_ACC
      ret.append(tuple(x for x in get_ranges_in_parents(u.src[1]) if x not in u.src[0].src[1:]))
    else:
      ret.append(get_ranges_in_parents(u))
  return tuple(dedup(sorted(flatten(ret), key=lambda x: x.tuplize)))

DONT_PLACE_IN_BLOCK = {Ops.RANGE, Ops.CONST, Ops.DEFINE_ACC, Ops.DEFINE_GLOBAL, Ops.DEFINE_VAR, Ops.SPECIAL}
def append_to_block(ctx, x:UOp):
  children, forks = ctx
  new_srcs = []
  to_append = []
  new_blocks: DefaultDict[Tuple[UOp, ...], List[UOp]] = defaultdict(list)
  updated = False
  block_uop_set = set(x.arg.lst)
  for u in x.src:
    if u.op is Ops.BLOCK:
      if len(new_block_list:=new_blocks[u.arg.rngs]): updated = True
      new_block_list.append(u)
    elif u.op in DONT_PLACE_IN_BLOCK or (len([y for y in children[u] if y not in block_uop_set]) and u not in forks):
      # it stays in srcs if it has children not in the basic or is RANGE/CONST
      new_srcs.append(u)
    else:
      updated = True
      if (rngs:=get_ranges_in_parents(u)) == x.arg.rngs:
        # fine to put it in this block
        new_srcs += list(u.src)
        to_append.append(u)
      else:
        # need to create a new block
        new_blocks[rngs].append(u)
  if not updated: return None
  for rng,lst in new_blocks.items():
    if len(lst) == 1 and lst[0].op is Ops.BLOCK:
      new_srcs.append(lst[0])
    else:
      new_lst = flatten([y.arg.lst if y.op is Ops.BLOCK else [y] for y in lst])
      new_srcs.append(UOp(Ops.BLOCK, dtypes.void, tuple(dedup(flatten(y.src for y in lst))), BasicBlock(rng, new_lst)))
  return UOp(Ops.BLOCK, dtypes.void, tuple(dedup(new_srcs)), x.arg.add(to_append))

make_basic_blocks = PatternMatcher([
  (UPat(Ops.SINK, name="x"), lambda x: UOp(Ops.BLOCK, dtypes.void, x.src, BasicBlock([], [x]))),
  (UPat(Ops.BLOCK, name="x"), append_to_block),
])

def get_children_dfs(u:UOp, children:Dict[UOp, List[UOp]], srcs:Dict[UOp, Dict[UOp, None]], in_degree:Dict[UOp, int]):
  if u in children: return srcs[u]
  srcs[u] = {}
  children[u] = []
  for x in u.src:
    srcs[u].update(get_children_dfs(x, children, srcs, in_degree))
    if x.op is Ops.RANGE and x.arg[1]: srcs[u][x] = None
    children[x].append(u)
  in_degree[u] = len(u.src)
  return srcs[u]

def block_uop(sink:UOp) -> UOp:
  children: Dict[UOp, List[UOp]] = {}
  range_srcs: Dict[UOp, Dict[UOp, None]] = {}
  in_degree: Dict[UOp, int] = {}
  get_children_dfs(sink, children, range_srcs, in_degree)

  forks: Dict[UOp, None] = {}
  while 1:
    sink = graph_rewrite(sink, make_basic_blocks, ctx=(children, forks))

    # some blocks have two children, find them and mark them as okay to fork
    forks = {}
    for block in sink.sparents:
      if block.op is not Ops.BLOCK: continue
      for u in block.src:
        if u.op is Ops.BLOCK or u.op in DONT_PLACE_IN_BLOCK: continue
        forks[u] = None
    if len(forks) == 0: break

  return sink

def linearize_uop(sink:UOp, skip_check:bool=not __debug__) -> List[UOp]:
  assert sink.op is Ops.SINK, f"sink isn't sink, it's {sink.op}"

  sink_bb = block_uop(sink)

  # filter nodes that don't link to a sink
  # BFS toposort
  children: Dict[UOp, List[UOp]] = {}
  range_srcs: Dict[UOp, Dict[UOp, None]] = {}
  in_degree: Dict[UOp, int] = {}
  get_children_dfs(sink_bb, children, range_srcs, in_degree)

  # NOTE: the compare should never make it all the way to u
  queue:List[Tuple[int, Tuple, UOp]] = []
  def push(u:UOp):
    p = 0
    if u.op is Ops.CONST: p = -10  # TODO: put CONST earlier
    heapq.heappush(queue, (p, u.tuplize, u))

  for u in children:
    if in_degree[u] == 0: push(u)

  _uops: List[UOp] = []
  open_loops: List[UOp] = []
  if DEBUG >= 6: print("*** PLACE")
  while queue:
    _,_,x = heapq.heappop(queue)
    if DEBUG >= 6: print(x.op, x.dtype, str(x.arg).split("\n")[0] if x.op is Ops.BLOCK else x.arg)
    if x.op is Ops.BLOCK:
      _uops.extend(x.arg.lst)
      for y in x.arg.lst:
        if y.op is Ops.IF: open_loops.append(y)
      # end any ranges
      for c in children[x]:
        assert c.op is Ops.BLOCK
        to_end = []
        for r in x.arg.rngs:
          assert r in open_loops, f"loop {r.arg} wasn't opened? {[x.arg for x in open_loops]} were"
          if r not in c.arg.rngs: to_end.append(r)
        for r in open_loops[::-1]:
          if r in to_end:
            _uops.append(UOp(Ops.ENDRANGE if r.op is Ops.RANGE else Ops.ENDIF, src=(r,)))
            open_loops.remove(r)
    elif x.op is Ops.DEFINE_ACC:
      idx = min([_uops.index(l) for l in x.src if l.op is Ops.RANGE])
      _uops.insert(idx, x)
    else:
      if x.op is Ops.RANGE: open_loops.append(x)
      _uops.append(x)
    for u in children[x]:
      in_degree[u] -= 1
      if in_degree[u] == 0: push(u)

  # sanity checks (NOTE: these can cause things to be skipped in BEAM)
  if not skip_check: type_verify(_uops)

  # strip the SINK
  assert _uops[-1].op is Ops.SINK
  return _uops[:-1]
