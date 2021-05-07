# Copyright 2019-2021 ETH Zurich and the DaCe authors. All rights reserved.
""" Automatic optimization routines for SDFGs. """

import dace
from dace.sdfg.state import SDFGState
from dace.sdfg.graph import SubgraphView
from dace.sdfg.propagation import propagate_states
from dace import config, data as dt, dtypes, Memlet, symbolic
from dace.sdfg import SDFG, nodes, graph as gr
from typing import Set, Tuple, Union, List, Iterable
import warnings

# Transformations
from dace.transformation.dataflow import MapCollapse, TrivialMapElimination, MapFusion, MapTiling, DeduplicateAccess
from dace.transformation.interstate import LoopToMap
from dace.transformation.subgraph.composite import CompositeFusion
from dace.transformation.subgraph import helpers, ReduceExpansion, StencilTiling

# Environments
from dace.libraries.blas.environments import intel_mkl as mkl, openblas

# Enumerator
from dace.transformation.estimator.enumeration import GreedyEnumerator

GraphViewType = Union[SDFG, SDFGState, gr.SubgraphView]


def greedy_fuse(
        graph_or_subgraph: GraphViewType,
        validate_all: bool,
        device: dace.dtypes.DeviceType = dace.dtypes.DeviceType.CPU,
        recursive: bool = True,
        tile: bool = False) -> None:
    '''
    Greedily fuses maps of an SDFG or graph, operating in-place.
    :param graph_or_subgraph: SDFG, SDFGState or Subgraph
    :param validate_all: Validate SDFG or graph at each fusion step 
    :param device: Device type to specialize for 
    :param recursive: Fuse recursively within (fused and unfused) scopes
    :param tile: Perform stencil fusion instead of regular fusion 
    '''

    if isinstance(graph_or_subgraph, SDFG):
        # If we have an SDFG, recurse into graphs
        graph_or_subgraph.apply_strict_transformations(
            validate_all=validate_all)
        # MapFusion for trivial cases
        graph_or_subgraph.apply_transformations_repeated(
            MapFusion, validate_all=validate_all)
        # recurse into graphs
        for graph in graph_or_subgraph.nodes():
            greedy_fuse(graph,
                        validate_all=validate_all,
                        device=device,
                        recursive=recursive,
                        tile=tile)
    else:
        # we are in graph or subgraph
        sdfg, graph, subgraph = None, None, None
        if isinstance(graph_or_subgraph, SDFGState):
            sdfg = graph_or_subgraph.parent
            sdfg.apply_transformations_repeated(MapFusion,
                                                validate_all=validate_all)
            graph = graph_or_subgraph
            subgraph = SubgraphView(graph, graph.nodes())
        else:
            sdfg = graph_or_subgraph.graph.parent
            graph = graph_or_subgraph.graph
            subgraph = graph_or_subgraph

        # within SDFGState: greedily enumerate fusible components
        # and apply transformation
        applied_transformations = 0
        reverse = True if tile else False
        enumerator = GreedyEnumerator(sdfg, graph, subgraph, reverse=reverse)

        if tile:
            CompositeFusion.allow_tiling._default = True
            CompositeFusion.schedule_innermaps._default = dtypes.ScheduleType.Sequential
            if device == dtypes.DeviceType.GPU:
                CompositeFusion.stencil_unroll_loops._default = True
        else:
            CompositeFusion.allow_tiling._default = False

        for map_entries in enumerator:
            if len(map_entries) > 1:
                current_subgraph = helpers.subgraph_from_maps(
                    sdfg, graph, map_entries)
                cf = CompositeFusion(current_subgraph)

                #cf.allow_expansion = apply_multi_expansion
                #cf.allow_tiling = apply_stencil_tiling
                cf.apply(sdfg)
                sdfg.validate()
                applied_transformations += 1
            if recursive:
                global_entry = cf._global_map_entry if len(
                    map_entries) > 1 else map_entries[0]
                greedy_fuse(graph.scope_subgraph(global_entry,
                                                 include_entry=False,
                                                 include_exit=False),
                            validate_all=validate_all)

        for node in graph_or_subgraph.nodes():
            if isinstance(node, nodes.NestedSDFG):
                greedy_fuse(node.sdfg,
                            validate_all=validate_all,
                            device=device,
                            tile=tile,
                            recursive=recursive)

        if applied_transformations > 0:
            if tile:
                print(f"Applied {applied_transformations} TileFusion")
            else:
                print(f"Applied {applied_transformations} SubgraphFusion")


        if validate_all:
            graph.validate()



def tile_wcrs(graph_or_subgraph: GraphViewType,
              validate_all: bool,
              prefer_partial_parallelism: bool = None) -> None:
    """
    Tiles parallel write-conflict resolution maps in an SDFG, state,
    or subgraphs thereof. Reduces the number of atomic operations by tiling
    and introducing transient arrays to accumulate atomics on.
    :param graph_or_subgraph: The SDFG/state/subgraph to optimize within.
    :param validate_all: If True, runs SDFG validation after every tiling.
    :param prefer_partial_parallelism: If set, prefers extracting non-conflicted
                                       map dimensions over tiling WCR map (may
                                       not perform well if parallel dimensions
                                       are small).
    :note: This function operates in-place.
    """
    # Avoid import loops
    from dace.codegen.targets import cpp
    from dace.frontend import operations
    from dace.transformation import dataflow, helpers as xfh

    # Determine on which nodes to run the operation
    graph = graph_or_subgraph
    if isinstance(graph_or_subgraph, gr.SubgraphView):
        graph = graph_or_subgraph.graph
    if isinstance(graph, SDFG):
        for state in graph_or_subgraph.nodes():
            tile_wcrs(state, validate_all)
        return
    if not isinstance(graph, SDFGState):
        raise TypeError(
            'Graph must be a state, an SDFG, or a subgraph of either')
    sdfg = graph.parent

    edges_to_consider: Set[Tuple[gr.MultiConnectorEdge[Memlet],
                                 nodes.EntryNode]] = set()
    for edge in graph_or_subgraph.edges():
        if edge.data.wcr is not None:
            if (isinstance(edge.src, (nodes.ExitNode, nodes.NestedSDFG))
                    or isinstance(edge.dst, nodes.EntryNode)):
                # Do not consider intermediate edges
                continue
            reason = cpp.is_write_conflicted_with_reason(graph, edge)
            if reason is None or not isinstance(reason, nodes.EntryNode):
                # Do not consider edges that will not generate atomics or
                # atomics we cannot transform
                continue
            if reason not in graph_or_subgraph.nodes():
                # Skip if conflict exists outside of nested SDFG
                continue

            # Check if identity value can be inferred
            redtype = operations.detect_reduction_type(edge.data.wcr)
            dtype = sdfg.arrays[edge.data.data].dtype
            identity = dtypes.reduction_identity(dtype, redtype)
            if identity is None:  # Cannot infer identity value
                continue

            edges_to_consider.add((edge, reason))

    tile_size = config.Config.get('optimizer', 'autotile_size')
    debugprint = config.Config.get_bool('debugprint')
    if prefer_partial_parallelism is None:
        prefer_partial_parallelism = config.Config.get_bool(
            'optimizer', 'autotile_partial_parallelism')

    maps_to_consider: Set[nodes.MapEntry] = set(me
                                                for _, me in edges_to_consider)

    transformed: Set[nodes.MapEntry] = set()

    # Heuristic: If the map is only partially conflicted, extract
    # parallel dimensions instead of tiling
    if prefer_partial_parallelism:
        for mapentry in maps_to_consider:
            # Check the write-conflicts of all WCR edges in map
            conflicts: Set[str] = set()
            for edge, me in edges_to_consider:
                if me is not mapentry:
                    continue
                conflicts |= set(cpp.write_conflicted_map_params(
                    mapentry, edge))

            nonconflicted_dims = set(mapentry.params) - conflicts
            if nonconflicted_dims:
                dims = [
                    i for i, p in enumerate(mapentry.params)
                    if p in nonconflicted_dims
                ]
                if ((dt._prod(s for i, s in enumerate(mapentry.range.size())
                              if i in dims) < tile_size) == True):
                    # Map has a small range, extracting parallelism may not be
                    # beneficial
                    continue
                xfh.extract_map_dims(sdfg, mapentry, dims)
                transformed.add(mapentry)

    # Tile and accumulate other not-transformed maps
    for edge, mapentry in edges_to_consider:
        if mapentry in transformed:
            continue
        transformed.add(mapentry)

        # NOTE: The test "(x < y) == True" below is crafted for SymPy
        # to be "definitely True"
        if all((s < tile_size) == True for s in mapentry.map.range.size()):
            # If smaller than tile size, don't transform and instead
            # make map sequential
            if debugprint:
                print(f'Making map "{mapentry}" sequential due to being '
                      'smaller than tile size')
            mapentry.map.schedule = dtypes.ScheduleType.Sequential
            continue

        # MapTiling -> AccumulateTransient / AccumulateStream
        outer_mapentry = dataflow.MapTiling.apply_to(
            sdfg, dict(tile_sizes=(tile_size, )), map_entry=mapentry)

        # Transform all outgoing WCR and stream edges
        mapexit = graph.exit_node(mapentry)
        outer_mapexit = graph.exit_node(outer_mapentry)
        for e in graph.out_edges(mapexit):
            if isinstance(sdfg.arrays[e.data.data], dt.Stream):
                mpath = graph.memlet_path(e)
                tasklet = mpath[0].src
                if not isinstance(tasklet, nodes.Tasklet) or len(mpath) != 3:
                    # TODO(later): Implement StreamTransient independently of tasklet
                    continue
                dataflow.StreamTransient.apply_to(sdfg,
                                                  tasklet=tasklet,
                                                  map_exit=mapexit,
                                                  outer_map_exit=outer_mapexit)
            else:
                if (e.data.is_empty() or e.data.wcr is None
                        or e.data.wcr_nonatomic
                        or (e.data.dst_subset is not None
                            and e.data.dst_subset.num_elements() != 0
                            and e.data.dynamic)):
                    continue

                dtype = sdfg.arrays[e.data.data].dtype
                redtype = operations.detect_reduction_type(e.data.wcr)
                identity = dtypes.reduction_identity(dtype, redtype)
                if identity is None:  # Cannot infer identity value
                    continue
                dataflow.AccumulateTransient.apply_to(
                    sdfg,
                    options=dict(identity=identity, array=e.data.data),
                    map_exit=mapexit,
                    outer_map_exit=outer_mapexit)

    if debugprint and len(transformed) > 0:
        print(f'Optimized {len(transformed)} write-conflicted maps')


def find_fast_library(device: dtypes.DeviceType) -> str:
    # Returns the optimized library node implementations for the given target
    # device
    if device is dtypes.DeviceType.GPU:
        return ['cuBLAS', 'CUB', 'pure']
    elif device is dtypes.DeviceType.CPU:
        result = []

        # BLAS calls
        if mkl.IntelMKL.is_installed():
            result.append('MKL')
        elif openblas.OpenBLAS.is_installed():
            result.append('OpenBLAS')

        return result + ['pure']

    return ['pure']


def move_small_arrays_to_stack(sdfg: SDFG) -> None:
    """
    Set all Default storage types that are constant sized and less than 
    the auto-tile size to the stack (as StorageType.Register).
    :param sdfg: The SDFG to operate on.
    :note: Operates in-place on the SDFG.
    """
    converted = 0
    tile_size = config.Config.get('optimizer', 'autotile_size')
    for sd, aname, array in sdfg.arrays_recursive():
        if isinstance(array, dt.Stream):
            continue
        if (array.transient and array.storage == dtypes.StorageType.Default
                and array.lifetime == dtypes.AllocationLifetime.Scope):
            if not symbolic.issymbolic(array.total_size, sd.constants):
                eval_size = symbolic.evaluate(array.total_size, sd.constants)
                try:
                    if eval_size <= tile_size:
                        array.storage = dtypes.StorageType.Register
                        converted += 1
                except TypeError:
                    # cannot determine truth value of relational
                    pass

    if config.Config.get_bool('debugprint') and converted > 0:
        print(f'Statically allocating {converted} transient arrays')


def set_fast_implementations(sdfg: SDFG,
                             device: dtypes.DeviceType,
                             blocklist: List[str] = [],
                             ignore_types: Iterable[type] = []):
    """
    Set fast library node implementations for the given device

    :param sdfg: The SDFG to optimize.
    :param device: the device to optimize for.
    :param blocklist: list of disallowed implementations.
    :param ignore_types: list of library node types to ignore
    :note: Operates in-place on the given SDFG.
    """
    if blocklist is None:
        implementation_prio = find_fast_library(device)
    else:
        implementation_prio = [
            i for i in find_fast_library(device) if i not in blocklist
        ]

    # pre_expand all specialized nodes
    for current_sdfg in sdfg.all_sdfgs_recursive():
        for state in current_sdfg.nodes():
            for node in state.nodes():
                if isinstance(node, nodes.LibraryNode):
                    if (node.default_implementation == 'specialize' and (len(
                            set(node.implementations)
                            & set(implementation_prio))) == 0):
                        print("Specializing node", node)
                        node.expand(current_sdfg, state)

    for node, _ in sdfg.all_nodes_recursive():
        if isinstance(node,
                      nodes.LibraryNode) and type(node) not in ignore_types:
            for impl in implementation_prio:
                if impl in node.implementations:
                    node.implementation = impl
                    break
            else:
                warnings.warn('No fast library implementation found for "%s", '
                              'falling back to default.' % node.name)


def auto_optimize(sdfg: SDFG,
                  device: dtypes.DeviceType,
                  validate: bool = True,
                  validate_all: bool = False,
                  symbols: dict = {},
                  trivial_fuse=True,
                  subgraph_fuse=True,
                  auto_parallelize=True) -> SDFG:
    """
    Runs a basic sequence of transformations to optimize a given SDFG to decent
    performance. In particular, performs the following:
        * Strict transformations
        * Strict auto-parallelization (loop-to-map)
        * Greedy application of SubgraphFusion
        * Tiled write-conflict resolution (MapTiling -> AccumulateTransient)
        * Tiled stream accumulation (MapTiling -> AccumulateTransient)
        * Collapse all maps to parallelize across all dimensions
        * Set all library nodes to expand to ``fast`` expansion, which calls
          the fastest library on the target device
    :param sdfg: The SDFG to optimize.
    :param device: the device to optimize for.
    :param validate: If True, validates the SDFG after all transformations
                     have been applied.
    :param validate_all: If True, validates the SDFG after every step.
    :param nofuse: Temporary Parameter to disable fusion in durbin/floyd
    :return: The optimized SDFG.
    :note: Operates in-place on the given SDFG.
    :note: This function is still experimental and may harm correctness in
           certain cases. Please report an issue if it does.
    """
    # Strict transformations
    sdfg.apply_strict_transformations(validate=False, validate_all=validate_all)

    # Try to eliminate trivial maps
    sdfg.apply_transformations_repeated(TrivialMapElimination,
                                        validate=validate,
                                        validate_all=validate_all)
    if auto_parallelize:
        # Try to parallelize loops
        for sd in sdfg.all_sdfgs_recursive():
            propagate_states(sd)
        strict_transformations = dace.transformation.strict_transformations()
        sdfg.apply_transformations_repeated([LoopToMap] +
                                            strict_transformations,
                                            strict=True,
                                            validate=False,
                                            validate_all=validate_all)
    # Collapse maps
    sdfg.apply_transformations_repeated(MapCollapse,
                                        strict=True,
                                        validate=False,
                                        validate_all=validate_all)

    # Apply GPU transformations and set library node implementations
    ignore_types = set()
    if device == dtypes.DeviceType.GPU:
        # Set library nodes
        for node, state in sdfg.all_nodes_recursive():
            if isinstance(node, dace.nodes.LibraryNode):
                from dace.sdfg.scope import is_devicelevel_gpu
                # Use CUB for device-level reductions
                if ('CUDA (device)' in node.implementations
                        and not is_devicelevel_gpu(state.parent, state, node)
                        and state.scope_dict()[node] is None):
                    node.implementation = 'CUDA (device)'
                    ignore_types.add(type(node))

        # apply gpu transformations
        sdfg.apply_gpu_transformations()

        # apply strict transformations
        sdfg.apply_strict_transformations()

    if subgraph_fuse:
        '''
        # expand reductions
        for graph in sdfg.nodes():
            for node in graph.nodes():
                if isinstance(node, dace.libraries.standard.nodes.Reduce):
                    if True: #graph.scope_dict()[node] is None:
                        try:
                            print("Expanding Reduction")
                            ReduceExpansion.apply_to(sdfg, _reduce = node, 
                                reduce_implementation = 'sequential')

                        except ValueError as e:
                            print("ValueError", e)
                            pass
        '''
        # fuse greedily
        greedy_fuse(sdfg, device=device, validate_all=validate_all)

        # tile greedily
        greedy_fuse(sdfg,
                    device=device,
                    validate_all=validate_all,
                    recursive=False,
                    tile=True)

    elif trivial_fuse:
        sdfg.apply_transformations_repeated(MapFusion)
    #sdfg.apply_transformations_repeated(DeduplicateAccess)
    #sdfg.apply_transformations(MapTiling)

    # Collapse maps
    sdfg.apply_transformations_repeated(MapCollapse,
                                        strict=True,
                                        validate=False,
                                        validate_all=validate_all)

    for node, _ in sdfg.all_nodes_recursive():
        if isinstance(node, nodes.MapEntry):
            #node.map.collapse = len(node.map.range) # TODO: try with as well
            pass

    # Set all library nodes to expand to fast library calls
    set_fast_implementations(sdfg, device, ignore_types=ignore_types)

    sdfg.expand_library_nodes()

    # Tiled WCR and streams
    for nsdfg in list(sdfg.all_sdfgs_recursive()):
        tile_wcrs(nsdfg, validate_all)

    # TODO(later): Safe vectorization

    # Disable OpenMP parallel sections on a per-SDFG basis
    for nsdfg in sdfg.all_sdfgs_recursive():
        nsdfg.openmp_sections = False

    # Specialize for all known symbols
    known_symbols = {
        s: v
        for (s, v) in symbols.items() if s in sdfg.free_symbols
    }
    if len(known_symbols) > 0:
        print("Specializing the SDFG for symbols", known_symbols)
    sdfg.specialize(known_symbols)

    # Set all Default storage types that are constant sized to registers
    move_small_arrays_to_stack(sdfg)

    for nsdfg in sdfg.all_sdfgs_recursive():
        for aname, arr in nsdfg.arrays.items():
            if arr.transient and not isinstance(
                    arr, dt.View) and not symbolic.issymbolic(arr.total_size):
                if arr.storage != dtypes.StorageType.Register:
                    arr.lifetime = dtypes.AllocationLifetime.Persistent

    if device == dtypes.DeviceType.GPU:
        for aname, arr in sdfg.arrays.items():
            if arr.transient and not isinstance(
                    arr, dt.View):  #and size only depends on SDFG params
                if arr.storage == dtypes.StorageType.GPU_Global:
                    arr.lifetime = dtypes.AllocationLifetime.Persistent

        # Reset nonatomic WCR edges
        for n, _ in sdfg.all_nodes_recursive():
            if isinstance(n, SDFGState):
                for edge in n.edges():
                    edge.data.wcr_nonatomic = False

    # Validate at the end
    if validate or validate_all:
        sdfg.validate()

    return sdfg
