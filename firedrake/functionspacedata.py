"""This module provides an object that encapsulates data that can be
shared between different :class:`~.FunctionSpace` objects.

The sharing is based on the idea of compatibility of function space
node layout.  The shared data is stored on the :func:`~.Mesh` the
function space is created on, since the created objects are
mesh-specific.  The sharing is done on an individual key basis.  So,
for example, Sets can be shared between all function spaces with the
same number of nodes per topological entity.  However, maps are
specific to the node *ordering*.

This means, for example, that function spaces with the same *node*
ordering, but different numbers of dofs per node (e.g. FiniteElement
vs VectorElement) can share the PyOP2 Set and Map data.
"""

import numpy
import finat
from decorator import decorator
from functools import reduce

from finat.finiteelementbase import entity_support_dofs

from coffee import base as ast

from pyop2 import op2
from pyop2.datatypes import IntType, as_cstr
from pyop2.utils import as_tuple

import firedrake.extrusion_numbering as extnum
from firedrake import halo as halo_mod
from firedrake import mesh as mesh_mod
from firedrake import extrusion_utils as eutils
from firedrake.petsc import PETSc


__all__ = ("get_shared_data", )


@decorator
def cached(f, mesh, key, *args, **kwargs):
    """Sui generis caching for a function whose data is
    associated with a mesh.

    :arg f: The function to cache.
    :arg mesh: The mesh to cache on (should have a
        ``_shared_data_cache`` object).
    :arg key: The key to the cache.
    :args args: Additional arguments to ``f``.
    :kwargs kwargs:  Additional keyword arguments to ``f``."""
    assert hasattr(mesh, "_shared_data_cache")
    cache = mesh._shared_data_cache[f.__name__]
    try:
        return cache[key]
    except KeyError:
        result = f(mesh, key, *args, **kwargs)
        cache[key] = result
        return result


@cached
def get_global_numbering(mesh, nodes_per_entity):
    """Get a PETSc Section describing the global numbering.

    This numbering associates function space nodes with topological
    entities.

    :arg mesh: The mesh to use.
    :arg nodes_per_entity: a tuple of the number of nodes per
        topological entity.
    :returns: A new PETSc Section.
    """
    return mesh.create_section(nodes_per_entity)


@cached
def get_node_set(mesh, nodes_per_entity):
    """Get the :class:`node set <pyop2.Set>`.

    :arg mesh: The mesh to use.
    :arg nodes_per_entity: The number of function space nodes per
        topological entity.
    :returns: A :class:`pyop2.Set` for the function space nodes.
    """
    global_numbering = get_global_numbering(mesh, nodes_per_entity)
    node_classes = mesh.node_classes(nodes_per_entity)
    halo = halo_mod.Halo(mesh._plex, global_numbering)
    node_set = op2.Set(node_classes, halo=halo, comm=mesh.comm)
    extruded = mesh.cell_set._extruded
    if extruded:
        # FIXME! This is a LIE! But these sets should not be extruded
        # anyway, only the code gen in PyOP2 is busted.
        node_set = op2.ExtrudedSet(node_set, layers=2)

    assert global_numbering.getStorageSize() == node_set.total_size
    if not extruded and node_set.total_size >= (1 << (IntType.itemsize * 8 - 4)):
        raise RuntimeError("Problems with more than %d nodes per process unsupported", (1 << (IntType.itemsize * 8 - 4)))
    return node_set


def get_cell_node_list(mesh, entity_dofs, global_numbering, offsets):
    """Get the cell->node list for specified dof layout.

    :arg mesh: The mesh to use.
    :arg entity_dofs: The FInAT entity_dofs dict.
    :arg global_numbering: The PETSc Section describing node layout
        (see :func:`get_global_numbering`).
    :arg offsets: layer offsets for each entity (maybe ignored).
    :returns: A numpy array mapping mesh cells to function space
        nodes.
    """
    return mesh.make_cell_node_list(global_numbering, entity_dofs, offsets)


def get_facet_node_list(mesh, kind, cell_node_list, offsets):
    """Get the facet->node list for specified dof layout.

    :arg mesh: The mesh to use.
    :arg kind: The facet kind (one of ``"interior_facets"`` or
        ``"exterior_facets"``).
    :arg cell_node_list: The map from mesh cells to function space
        nodes, see :func:`get_cell_node_list`.
    :arg offsets: layer offsets for each entity (maybe ignored).
    :returns: A numpy array mapping mesh facets to function space
        nodes.
    """
    assert kind in ["interior_facets", "exterior_facets"]
    if mesh._plex.getStratumSize(kind, 1) > 0:
        return mesh.make_facet_node_list(cell_node_list, kind, offsets)
    else:
        return numpy.array([], dtype=IntType)


@cached
def get_entity_node_lists(mesh, key, entity_dofs, global_numbering, offsets):
    """Get the map from mesh entity sets to function space nodes.

    :arg mesh: The mesh to use.
    :arg key: Canonicalised entity_dofs (see :func:`entity_dofs_key`).
    :arg entity_dofs: FInAT entity dofs.
    :arg global_numbering: The PETSc Section describing node layout
        (see :func:`get_global_numbering`).
    :arg offsets: layer offsets for each entity (maybe ignored).
    :returns: A dict mapping mesh entity sets to numpy arrays of
        function space nodes.
    """
    # set->node lists are specific to the sorted entity_dofs.
    cell_node_list = get_cell_node_list(mesh, entity_dofs, global_numbering, offsets)
    interior_facet_node_list = get_facet_node_list(mesh, "interior_facets", cell_node_list, offsets)
    exterior_facet_node_list = get_facet_node_list(mesh, "exterior_facets", cell_node_list, offsets)
    return {mesh.cell_set: cell_node_list,
            mesh.interior_facets.set: interior_facet_node_list,
            mesh.exterior_facets.set: exterior_facet_node_list}


@cached
def get_map_caches(mesh, entity_dofs):
    """Get the map caches for this mesh.

    :arg mesh: The mesh to use.
    :arg entity_dofs: Canonicalised entity_dofs (see
        :func:`entity_dofs_key`).
    """
    return {mesh.cell_set: {},
            mesh.interior_facets.set: {},
            mesh.exterior_facets.set: {},
            "boundary_node": {}}


@cached
def get_dof_offset(mesh, key, entity_dofs, ndof):
    """Get the dof offsets.

    :arg mesh: The mesh to use.
    :arg key: Canonicalised entity_dofs (see :func:`entity_dofs_key`).
    :arg entity_dofs: The FInAT entity_dofs dict.
    :arg ndof: The number of dofs (the FInAT space_dimension).
    :returns: A numpy array of dof offsets (extruded) or ``None``.
    """
    return mesh.make_offset(entity_dofs, ndof)


@cached
def get_boundary_masks(mesh, key, finat_element):
    """Get masks for facet dofs.

    :arg mesh: The mesh to use.
    :arg key: Canonicalised entity_dofs (see :func:`entity_dofs_key`).
    :arg finat_element: The FInAT element.
    :returns: A dict mapping ``"topological"`` and ``"geometric"``
        keys to boundary nodes or ``None``.  If not None, the entry in
        the mask dict is an array of shape `(nfacet, ndof)` with the
        ordering that of the reference cell topology.  Each `ndof`
        entry is a True/False value that indicates whether that dof
        is in the closure of the relevant facet.
    """
    if not mesh.cell_set._extruded:
        return None
    masks = {}
    dim = finat_element.cell.get_spatial_dimension()
    ecd = finat_element.entity_closure_dofs()
    try:
        esd = finat_element.entity_support_dofs()
    except NotImplementedError:
        # 4-D cells
        esd = None
    # Number of entities on cell excepting the cell itself.
    chart = sum(map(len, ecd.values())) - 1
    closure_section = PETSc.Section().create(comm=PETSc.COMM_SELF)
    support_section = PETSc.Section().create(comm=PETSc.COMM_SELF)
    closure_section.setChart(0, chart)
    support_section.setChart(0, chart)
    closure_indices = []
    support_indices = []
    facet_points = []
    p = 0
    for ent in sorted(ecd.keys()):
        # Never need closure of cell
        if sum(ent) == dim:
            continue
        for key in sorted(ecd[ent].keys()):
            closure_section.setDof(p, len(ecd[ent][key]))
            closure_indices.extend(sorted(ecd[ent][key]))
            if esd is not None:
                support_section.setDof(p, len(esd[ent][key]))
                support_indices.extend(sorted(esd[ent][key]))
            if sum(ent) == dim - 1:
                facet_points.append(p)
            p += 1
    closure_section.setUp()
    support_section.setUp()
    closure_indices = numpy.asarray(closure_indices, dtype=IntType)
    support_indices = numpy.asarray(support_indices, dtype=IntType)
    facet_points = numpy.asarray(facet_points, dtype=IntType)
    masks["topological"] = op2.Map.MapMask(closure_section, closure_indices, facet_points)
    masks["geometric"] = op2.Map.MapMask(support_section, support_indices, facet_points)
    return masks


@cached
def get_work_function_cache(mesh, ufl_element):
    """Get the cache for work functions.

    :arg mesh: The mesh to use.
    :arg ufl_element: The ufl element, used as a key.
    :returns: A dict.

    :class:`.FunctionSpace` objects sharing the same UFL element (and
    therefore comparing equal) share a work function cache.
    """
    return {}


@cached
def get_top_bottom_boundary_nodes(mesh, key, V):
    """Get top or bottom boundary nodes of an extruded function space.

    :arg mesh: The mesh to cache on.
    :arg key: The key a 3-tuple of ``(entity_dofs_key, sub_domain, method)``.
        Where sub_domain indicates top or bottom and method is whether
        we should identify dofs on facets topologically or geometrically.
    :arg V: The FunctionSpace to select from.
    :arg entity_dofs: The flattened entity dofs.
    :returnsL: A numpy array of the (unique) boundary nodes.
    """
    _, sub_domain, method = key
    cell_node_list = V.cell_node_list
    offset = V.offset
    if mesh.variable_layers:
        if method == "geometric":
            raise NotImplementedError("Generic entity_support_dofs not implemented.")
        return extnum.top_bottom_boundary_nodes(mesh, cell_node_list,
                                                V.boundary_masks[method],
                                                offset,
                                                sub_domain)
    else:
        idx = {"bottom": -2, "top": -1}[sub_domain]
        section, indices, facet_points = V.boundary_masks[method]
        facet = facet_points[idx]
        dof = section.getDof(facet)
        off = section.getOffset(facet)
        mask = indices[off:off+dof]
        nodes = cell_node_list[..., mask]
        if sub_domain == "top":
            nodes = nodes + offset[mask]*(mesh.cell_set.layers - 2)
        return numpy.unique(nodes)


@cached
def get_boundary_nodes(mesh, key, V):
    _, sub_domain, method = key
    if mesh.variable_layers:
        indices = extnum.boundary_nodes(V, sub_domain, method)
    else:
        nodes = V.exterior_facet_boundary_node_map(method).values_with_halo
        if sub_domain != "on_boundary":
            indices = mesh.exterior_facets.subset(sub_domain).indices
            nodes = nodes.take(indices, axis=0)
            if not V.extruded:
                indices = numpy.unique(nodes)
            else:
                offset = V.exterior_facet_boundary_node_map(method).offset
                indices = numpy.unique(numpy.concatenate([nodes + i * offset
                                                          for i in range(mesh.cell_set.layers - 1)]))
    # We need a halo exchange to determine all bc nodes.
    # Should be improved by doing this on the DM topology once.
    d = op2.Dat(V.dof_dset.set, dtype=IntType)
    d.data_with_halos[indices] = 1
    d.global_to_local_begin(op2.READ)
    d.global_to_local_end(op2.READ)
    indices, = numpy.where(d.data_ro_with_halos == 1)
    # cast, because numpy where returns an int64
    return indices.astype(IntType)


def get_max_work_functions(V):
    """Get the maximum number of work functions.

    :arg V: The function space to get the number of work functions for.
    :returns: The maximum number of work functions.

    This number is shared between all function spaces with the same
    :meth:`~.FunctionSpace.ufl_element` and
    :meth:`~FunctionSpace.mesh`.

    The default is 25 work functions per function space.  This can be
    set using :func:`set_max_work_functions`.
    """
    mesh = V.mesh()
    assert hasattr(mesh, "_shared_data_cache")
    cache = mesh._shared_data_cache["max_work_functions"]
    return cache.get(V.ufl_element(), 25)


def set_max_work_functions(V, val):
    """Set the maximum number of work functions.

    :arg V: The function space to set the number of work functions
        for.
    :arg val: The new maximum number of work functions.

    This number is shared between all function spaces with the same
    :meth:`~.FunctionSpace.ufl_element` and
    :meth:`~FunctionSpace.mesh`.
    """
    mesh = V.mesh()
    assert hasattr(mesh, "_shared_data_cache")
    cache = mesh._shared_data_cache["max_work_functions"]
    cache[V.ufl_element()] = val


def entity_dofs_key(entity_dofs):
    """Provide a canonical key for an entity_dofs dict.

    :arg entity_dofs: The FInAT entity_dofs.
    :returns: A tuple of canonicalised entity_dofs (suitable for
        caching).
    """
    key = []
    for k in sorted(entity_dofs.keys()):
        sub_key = [k]
        for sk in sorted(entity_dofs[k]):
            sub_key.append(tuple(entity_dofs[k][sk]))
        key.append(tuple(sub_key))
    key = tuple(key)
    return key


class FunctionSpaceData(object):
    """Function spaces with the same entity dofs share data.  This class
    stores that shared data.  It is cached on the mesh.

    :arg mesh: The mesh to share the data on.
    :arg finat_element: The FInAT element describing how nodes are
       attached to topological entities.
    """
    __slots__ = ("map_caches", "entity_node_lists",
                 "node_set", "boundary_masks", "offset",
                 "extruded", "mesh", "global_numbering")

    def __init__(self, mesh, finat_element):
        entity_dofs = finat_element.entity_dofs()
        nodes_per_entity = tuple(mesh.make_dofs_per_plex_entity(entity_dofs))

        # Create the PetscSection mapping topological entities to functionspace nodes
        # For non-scalar valued function spaces, there are multiple dofs per node.

        # These are keyed only on nodes per topological entity.
        global_numbering = get_global_numbering(mesh, nodes_per_entity)
        node_set = get_node_set(mesh, nodes_per_entity)

        edofs_key = entity_dofs_key(entity_dofs)

        # Empty map caches. This is a sui generis cache
        # implementation because of the need to support boundary
        # conditions.
        # Map caches are specific to a cell_node_list, which is keyed by entity_dof
        self.map_caches = get_map_caches(mesh, edofs_key)
        self.offset = get_dof_offset(mesh, edofs_key, entity_dofs, finat_element.space_dimension())
        self.entity_node_lists = get_entity_node_lists(mesh, edofs_key, entity_dofs, global_numbering, self.offset)
        self.node_set = node_set
        self.boundary_masks = get_boundary_masks(mesh, edofs_key, finat_element)
        self.extruded = mesh.cell_set._extruded
        self.mesh = mesh
        self.global_numbering = global_numbering

    def __eq__(self, other):
        if type(self) is not type(other):
            return False
        return all(getattr(self, s) is getattr(other, s) for s in
                   FunctionSpaceData.__slots__)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "FunctionSpaceData(%r, %r)" % (self.mesh, self.node_set)

    def __str__(self):
        return "FunctionSpaceData(%s, %s)" % (self.mesh, self.node_set)

    def exterior_facet_boundary_node_map(self, V, method):
        """Return the :class:`pyop2.Map` from exterior facets to nodes
        on the boundary.

        :arg V: The function space.
        :arg method:  The method for determining boundary nodes.  See
           :class:`~.DirichletBC` for details.
        """
        try:
            return self.map_caches["boundary_node"][method]
        except KeyError:
            pass
        el = V.finat_element

        dim = self.mesh.facet_dimension()

        if method == "topological":
            boundary_dofs = el.entity_closure_dofs()[dim]
        elif method == "geometric":
            # This function is only called on extruded meshes when
            # asking for the nodes that live on the "vertical"
            # exterior facets.
            boundary_dofs = entity_support_dofs(el, dim)

        nodes_per_facet = \
            len(boundary_dofs[0])

        # HACK ALERT
        # The facet set does not have a halo associated with it, since
        # we only construct halos for DoF sets.  Fortunately, this
        # loop is direct and we already have all the correct
        # information available locally.  So We fake a set of the
        # correct size and carry out a direct loop
        facet_set = op2.Set(self.mesh.exterior_facets.set.total_size,
                            comm=self.mesh.comm)

        fs_dat = op2.Dat(facet_set**el.space_dimension(),
                         data=V.exterior_facet_node_map().values_with_halo.view())

        facet_dat = op2.Dat(facet_set**nodes_per_facet,
                            dtype=IntType)

        # Ensure these come out in sorted order.
        local_facet_nodes = numpy.array(
            [boundary_dofs[e] for e in sorted(boundary_dofs.keys())])

        # Helper function to turn the inner index of an array into c
        # array literals.
        c_array = lambda xs: "{"+", ".join(map(str, xs))+"}"

        # AST for: l_nodes[facet[0]][n]
        rank_ast = ast.Symbol("l_nodes", rank=(ast.Symbol("facet", rank=(0,)), "n"))

        body = ast.Block([ast.Decl("int",
                                   ast.Symbol("l_nodes", (len(el.cell.topology[dim]),
                                                          nodes_per_facet)),
                                   init=ast.ArrayInit(c_array(map(c_array, local_facet_nodes))),
                                   qualifiers=["const"]),
                          ast.For(ast.Decl("int", "n", 0),
                                  ast.Less("n", nodes_per_facet),
                                  ast.Incr("n", 1),
                                  ast.Assign(ast.Symbol("facet_nodes", ("n",)),
                                             ast.Symbol("cell_nodes", (rank_ast, ))))
                          ])

        kernel = op2.Kernel(ast.FunDecl("void", "create_bc_node_map",
                                        [ast.Decl("%s*" % as_cstr(fs_dat.dtype),
                                                  "cell_nodes"),
                                         ast.Decl("%s*" % as_cstr(facet_dat.dtype),
                                                  "facet_nodes"),
                                         ast.Decl("unsigned int*", "facet")],
                                        body),
                            "create_bc_node_map")

        local_facet_dat = op2.Dat(facet_set ** self.mesh.exterior_facets._rank,
                                  self.mesh.exterior_facets.local_facet_dat.data_ro_with_halos,
                                  dtype=numpy.uintc)
        if self.extruded:
            offset = self.offset[boundary_dofs[0]]
            if self.mesh.variable_layers:
                raise NotImplementedError("Variable layer case not handled, should never reach here")
        else:
            offset = None
        op2.par_loop(kernel, facet_set,
                     fs_dat(op2.READ),
                     facet_dat(op2.WRITE),
                     local_facet_dat(op2.READ))

        val = op2.Map(facet_set, self.node_set,
                      nodes_per_facet,
                      facet_dat.data_ro_with_halos,
                      name="exterior_facet_boundary_node",
                      offset=offset)
        self.map_caches["boundary_node"][method] = val
        return val

    def boundary_nodes(self, V, sub_domain, method):
        if method not in {"topological", "geometric"}:
            raise ValueError("Don't know how to extract nodes with method '%s'", method)
        if sub_domain in ["bottom", "top"]:
            if not V.extruded:
                raise ValueError("Invalid subdomain '%s' for non-extruded mesh",
                                 sub_domain)
            entity_dofs = eutils.flat_entity_dofs(V.finat_element.entity_dofs())
            key = (entity_dofs_key(entity_dofs), sub_domain, method)
            return get_top_bottom_boundary_nodes(V.mesh(), key, V)
        else:
            if sub_domain == "on_boundary":
                sdkey = sub_domain
            else:
                sdkey = as_tuple(sub_domain)
            key = (entity_dofs_key(V.finat_element.entity_dofs()), sdkey, method)
            return get_boundary_nodes(V.mesh(), key, V)

    def get_map(self, V, entity_set, map_arity, bcs, name, offset, parent,
                kind=None):
        """Return a :class:`pyop2.Map` from some topological entity to
        degrees of freedom.

        :arg V: The :class:`FunctionSpace` to create the map for.
        :arg entity_set: The :class:`pyop2.Set` of entities to map from.
        :arg map_arity: The arity of the resulting map.
        :arg bcs: An iterable of :class:`~.DirichletBC` objects (may
            be ``None``.
        :arg name: A name for the resulting map.
        :arg offset: Map offset (for extruded).
        :arg parent: The parent map (used when bcs are provided)."""
        # V is only really used for error checking and "name".
        assert len(V) == 1, "get_map should not be called on MixedFunctionSpace"
        entity_node_list = self.entity_node_lists[entity_set]

        if bcs is not None:
            # Separate explicit bcs (we just place negative entries in
            # the appropriate map values) from implicit ones (extruded
            # top and bottom) that require PyOP2 code gen.
            explicit_bcs = [bc for bc in bcs if bc.sub_domain not in ['top', 'bottom']]
            implicit_bcs = [(bc.sub_domain, bc.method) for bc in bcs if bc.sub_domain in ['top', 'bottom']]
            if len(explicit_bcs) == 0:
                # Implicit bcs are not part of the cache key for the
                # map (they only change the generated PyOP2 code),
                # hence rewrite bcs here.
                bcs = ()
            if len(implicit_bcs) == 0:
                implicit_bcs = None
        else:
            # Empty tuple if no bcs found.  This is so that matrix
            # assembly, which uses a set to keep track of the bcs
            # applied to matrix hits the cache when that set is
            # empty.  tuple(set([])) == tuple().
            bcs = ()
            implicit_bcs = None

        for bc in bcs:
            fs = bc.function_space()
            # Unwind proxies for ComponentFunctionSpace, but not
            # IndexedFunctionSpace.
            while fs.component is not None and fs.parent is not None:
                fs = fs.parent
            if fs.topological != V:
                raise RuntimeError("DirichletBC defined on a different FunctionSpace!")
        # Ensure bcs is a tuple in a canonical order for the hash key.
        lbcs = tuple(sorted(bcs, key=lambda bc: bc.__hash__()))

        cache = self.map_caches[entity_set]
        try:
            # Cache hit
            val = cache[lbcs]
            # In the implicit bc case, we decorate the cached map with
            # the list of implicit boundary conditions so PyOP2 knows
            # what to do.
            if implicit_bcs:
                val = op2.DecoratedMap(val, implicit_bcs=implicit_bcs)
            return val
        except KeyError:
            # Cache miss.
            # Any top and bottom bcs (for the extruded case) are handled elsewhere.
            nodes = [bc.nodes for bc in lbcs if bc.sub_domain not in ['top', 'bottom']]
            decorate = any(bc.function_space().component is not None for
                           bc in lbcs)
            if nodes:
                bcids = reduce(numpy.union1d, nodes)
                negids = numpy.copy(bcids)
                for bc in lbcs:
                    if bc.sub_domain in ["top", "bottom"]:
                        continue
                    nbits = IntType.itemsize * 8 - 2
                    if decorate and bc.function_space().component is None:
                        # Some of the other entries will be marked
                        # with high bits, so we need to set all the
                        # high bits for these bcs
                        idx = numpy.searchsorted(bcids, bc.nodes)
                        if bc.function_space().value_size > 3:
                            raise ValueError("Can't have component BCs with more than three components (have %d)", bc.function_space().value_size)
                        for cmp in range(bc.function_space().value_size):
                            negids[idx] |= (1 << (nbits - cmp))

                    # FunctionSpace with component is IndexedVFS
                    if bc.function_space().component is not None:
                        # For indexed VFS bcs, we encode the component
                        # in the high bits of the map value.
                        # That value is then negated to indicate to
                        # the generated code to discard the values
                        #
                        # So here we do:
                        #
                        # node = -(node + 2**(nbits-cmpt) + 1)
                        #
                        # And in the generated code we can then
                        # extract the information to discard the
                        # correct entries.
                        # bcids is sorted, so use searchsorted to find indices
                        idx = numpy.searchsorted(bcids, bc.nodes)
                        # Set appropriate bit
                        negids[idx] |= (1 << (nbits - bc.function_space().component))
                node_list_bc = numpy.arange(self.node_set.total_size,
                                            dtype=IntType)
                # Fix up for extruded, doesn't commute with indexedvfs
                # for now
                if self.extruded:
                    node_list_bc[bcids] = -10000000
                else:
                    node_list_bc[bcids] = -(negids + 1)
                new_entity_node_list = node_list_bc.take(entity_node_list)
            else:
                new_entity_node_list = entity_node_list

            # TODO: handle kind == interior_facet
            val = op2.Map(entity_set, self.node_set,
                          map_arity,
                          new_entity_node_list,
                          ("%s_"+name) % (V.name),
                          offset=offset,
                          parent=parent,
                          boundary_masks=self.boundary_masks)

            if decorate:
                val = op2.DecoratedMap(val, vector_index=True)
            cache[lbcs] = val
            if implicit_bcs:
                return op2.DecoratedMap(val, implicit_bcs=implicit_bcs)
            return val


def get_shared_data(mesh, finat_element):
    """Return the :class:`FunctionSpaceData` for the given
    element.

    :arg mesh: The mesh to build the function space data on.
    :arg finat_element: A FInAT element.
    :raises ValueError: if mesh or finat_element are invalid.
    :returns: a :class:`FunctionSpaceData` object with the shared
        data.
    """
    if not isinstance(mesh, mesh_mod.MeshTopology):
        raise ValueError("%s is not a MeshTopology" % mesh)
    if not isinstance(finat_element, finat.finiteelementbase.FiniteElementBase):
        raise ValueError("Can't create function space data from a %s" %
                         type(finat_element))
    return FunctionSpaceData(mesh, finat_element)
