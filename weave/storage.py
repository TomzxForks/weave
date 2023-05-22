import os
import re
import typing
import pathlib
import functools

from . import errors
from . import ref_base
from . import artifact_base
from . import artifact_mem
from . import artifact_fs
from . import artifact_local
from . import artifact_wandb
from . import weave_types as types
from . import mappers_python
from . import box
from . import errors
from . import graph

Ref = ref_base.Ref


def split_path_dotfile(path, dotfile_name):
    while path != "/":
        path, tail = os.path.split(path)
        if os.path.exists(os.path.join(path, dotfile_name)):
            return path, tail
    raise FileNotFoundError


def _get_name(wb_type: types.Type, obj: typing.Any) -> str:
    return wb_type.name
    # This tries to figure out which variable references obj.
    # But it is slow when there are a lot of references. If we want to do
    # something like this, we'll need to do it somewhere closer to user
    # interaction.
    # obj_names = util.find_names(obj)
    # return f"{wb_type.name}-{obj_names[-1]}"


def _get_weave_type(obj: typing.Any):
    try:
        return types.TypeRegistry.type_of(obj)
    except errors.WeaveTypeError as e:
        raise errors.WeaveSerializeError(
            "weave type error during serialization for object: %s. %s"
            % (obj, str(e.args))
        )


def _ensure_object_components_are_published(
    obj: typing.Any, wb_type: types.Type, artifact: artifact_wandb.WandbArtifact
):
    from weave.mappers_publisher import map_to_python_remote

    mapper = map_to_python_remote(wb_type, artifact)
    return mapper.apply(obj)


def _update_weave_meta(wb_type: types.Type, artifact: artifact_wandb.WandbArtifact):
    panel_type = types.type_name_to_type("Panel")
    artifact._writeable_artifact.metadata["_weave_meta"] = {
        "is_weave_obj": True,
        "type_name": wb_type.name,
        "is_panel": panel_type and panel_type().assign_type(wb_type),
    }


def _assert_valid_name_part(part: typing.Optional[str] = None):
    if part is None:
        return
    # if not re.match(r"^[a-zA-Z0-9_\-.]+$", part): # from W&B Artifacts
    if not re.match(r"^[a-zA-Z0-9_\-]+$", part):
        raise ValueError(
            "Invalid name part %s. Must be alphanumeric, dashes, or underscores." % part
        )


def _direct_publish(
    obj: typing.Any,
    name: typing.Optional[str] = None,
    wb_project_name: typing.Optional[str] = None,
    wb_artifact_type_name: typing.Optional[str] = None,
    wb_entity_name: typing.Optional[str] = None,
    branch_name: typing.Optional[str] = None,
    assume_weave_type: typing.Optional[types.Type] = None,
):
    weave_type = assume_weave_type or _get_weave_type(obj)

    wb_project_name = wb_project_name or artifact_wandb.DEFAULT_WEAVE_OBJ_PROJECT
    name = name or _get_name(weave_type, obj)
    wb_artifact_type_name = wb_artifact_type_name or weave_type.name

    _assert_valid_name_part(name)
    _assert_valid_name_part(wb_project_name)
    _assert_valid_name_part(wb_artifact_type_name)
    _assert_valid_name_part(branch_name)
    _assert_valid_name_part(wb_entity_name)
    # Validate entity name once we have them

    obj = box.box(obj)
    artifact = artifact_wandb.WandbArtifact(name, type=wb_artifact_type_name)
    obj = _ensure_object_components_are_published(obj, weave_type, artifact)
    _update_weave_meta(weave_type, artifact)
    ref = artifact.set("obj", weave_type, obj)

    # Only save if we have a ref into the artifact we created above. Otherwise
    #     nothing new was created, so just return the existing ref.
    if ref.artifact == artifact:
        artifact.save(
            project=wb_project_name, entity_name=wb_entity_name, branch=branch_name
        )

    return ref


def _direct_save(
    obj: typing.Any,
    name: typing.Optional[str] = None,
    branch_name: typing.Optional[str] = None,
    source_branch_name: typing.Optional[str] = None,
    assume_weave_type: typing.Optional[types.Type] = None,
    artifact: typing.Optional[artifact_local.LocalArtifact] = None,
):
    weave_type = assume_weave_type or _get_weave_type(obj)
    name = name or _get_name(weave_type, obj)

    _assert_valid_name_part(name)
    _assert_valid_name_part(branch_name)
    _assert_valid_name_part(source_branch_name)

    obj = box.box(obj)
    if artifact is None:
        # Using `version=source_branch_name` feels too magical. Would be better
        # to have a classmethod on LocalArtifact that takes a branch name and
        # returns an artifact with that branch name. This also precludes
        # directly saving an artifact with a branchpoint that is not local
        artifact = artifact_local.LocalArtifact(name, version=source_branch_name)
    ref = artifact.set("obj", weave_type, obj)

    # Only save if we have a ref into the artifact we created above. Otherwise
    #     nothing new was created, so just return the existing ref.
    if ref.artifact == artifact:
        artifact.save(branch=branch_name)

    return ref


def publish(obj, name=None, type=None):
    # We would probably refactor this method to be more like _direct_publish. This effectively
    # just a wrapper that let's the user specify project name with a slash.
    # TODO: should we only expose save for our API with a "remote" flag or something
    project_name = None
    if name is not None and "/" in name:
        project_name, name = name.split("/")

    return _direct_publish(
        obj,
        name=name,
        wb_project_name=project_name,
        assume_weave_type=type,
    )


def save(
    obj,
    name=None,
    type=None,
    artifact=None,
    branch=None,
) -> artifact_local.LocalArtifactRef:
    # We would probably refactor this method to be more like _direct_save. This effectively
    # just a wrapper that let's the user specify source_branch name with a slash.
    source_branch = None
    if name is not None and ":" in name:
        name, source_branch = name.split(":", 1)

    return _direct_save(
        obj=obj,
        name=name,
        branch_name=branch,
        source_branch_name=source_branch,
        assume_weave_type=type,
        artifact=artifact,
    )


def get(uri_s: typing.Union[str, ref_base.Ref]) -> typing.Any:
    if isinstance(uri_s, ref_base.Ref):
        return uri_s.get()
    return ref_base.Ref.from_str(uri_s).get()


get_local_version_ref = artifact_local.get_local_version_ref
get_local_version = artifact_local.get_local_version


def deref(ref):
    if isinstance(ref, ref_base.Ref):
        return ref.get()
    return ref


def _get_ref(obj: typing.Any) -> typing.Optional[ref_base.Ref]:
    if isinstance(obj, ref_base.Ref):
        return obj
    return ref_base.get_ref(obj)


def clear_ref(obj):
    ref_base.clear_ref(obj)


get_ref = _get_ref


# Return all local artifacts.
# Warning: may iterate a lot of the filesystem!
def local_artifacts() -> typing.List[artifact_local.LocalArtifact]:
    result = []
    obj_paths = sorted(
        pathlib.Path(artifact_local.local_artifact_dir()).iterdir(),
        key=os.path.getctime,
    )
    for art_path in obj_paths:
        if os.path.basename(art_path) != "tmp":
            result.append(artifact_local.LocalArtifact(art_path.name, None))
    return result


def all_objects():
    result = []
    obj_paths = sorted(
        pathlib.Path(artifact_local.local_artifact_dir()).iterdir(),
        key=os.path.getctime,
    )
    for art_path in obj_paths:
        ref = artifact_local.get_local_version_ref(art_path.name, "latest")
        if ref is not None:
            result.append((ref.created_at, ref))
    return [r[1] for r in sorted(result)]


def objects(
    of_type: types.Type, alias: str = "latest"
) -> typing.List[artifact_local.LocalArtifactRef]:
    result = []
    for art_name in os.listdir(artifact_local.local_artifact_dir()):
        try:
            ref = artifact_local.get_local_version_ref(art_name, alias)
            if ref is not None:
                if of_type.assign_type(ref.type):
                    # TODO: Why did I have this here?
                    # obj = ref.get()
                    # if isinstance(ref.type, types.RunType) and obj.op_name == "op-objects":
                    #     continue
                    result.append((ref.artifact.created_at, ref))
        except errors.WeaveSerializeError:
            # This happens because we may not have loaded ecosystem stuff that we need
            # to deserialize
            continue
    # Sorted by created_at
    return [r[1] for r in sorted(result)]


def recursively_unwrap_arrow(obj):
    if getattr(obj, "to_pylist_notags", None):
        return obj.to_pylist_notags()
    if getattr(obj, "as_py", None):
        return obj.as_py()
    if isinstance(obj, graph.Node):
        return obj
    if isinstance(obj, dict):
        return {k: recursively_unwrap_arrow(v) for (k, v) in obj.items()}
    elif isinstance(obj, list):
        return [recursively_unwrap_arrow(item) for item in obj]
    return obj


def to_python(obj, wb_type=None):
    if wb_type is None:
        wb_type = types.TypeRegistry.type_of(obj)

    # First map the object using a MemArtifact to capture any custom object refs.
    art = artifact_mem.MemArtifact()
    mapper = mappers_python.map_to_python(wb_type, art)
    val = mapper.apply(obj)

    if art.ref_count() > 0:
        # There are custom objects, create a local artifact to persist them.
        fs_art = artifact_local.LocalArtifact(wb_type.name, "latest")
        # Save all the reffed objects into the new artifact.
        for mem_ref in art.refs():
            fs_art.set(mem_ref.path, mem_ref._type, mem_ref._obj)
        fs_art.save()
        # now map the original object again. Because there are now existing refs
        # to the local artifact for any custom objects, this new value will contain
        # those existing refs as absolute refs. We provide None for artifact because
        # it should not be used in this pass.
        mapper = mappers_python.map_to_python(wb_type, None)
        val = mapper.apply(obj)
    # TODO: this should be a ConstNode!
    return {"_type": wb_type.to_dict(), "_val": val}


def to_safe_const(obj):
    wb_type = types.TypeRegistry.type_of(obj)
    mapper = mappers_python.map_to_python(wb_type, artifact_mem.MemArtifact())
    val = mapper.apply(obj)
    return graph.ConstNode(wb_type, val)


def from_python(obj, wb_type=None):
    if wb_type is None:
        wb_type = types.TypeRegistry.type_from_dict(obj["_type"])
    mapper = mappers_python.map_from_python(wb_type, None)
    res = mapper.apply(obj["_val"])
    return res


def make_js_serializer():
    artifact = artifact_mem.MemArtifact()
    return functools.partial(to_weavejs, artifact=artifact)


def to_weavejs(obj, artifact: typing.Optional[artifact_base.Artifact] = None):
    from .ops_arrow import list_ as arrow_list

    obj = box.unbox(obj)
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    elif isinstance(obj, dict):
        return {k: to_weavejs(v, artifact=artifact) for (k, v) in obj.items()}
    elif isinstance(obj, list):
        return [to_weavejs(item, artifact=artifact) for item in obj]
    elif isinstance(obj, arrow_list.ArrowWeaveList):
        return obj.to_pylist_notags()
    wb_type = types.TypeRegistry.type_of(obj)

    if artifact is None:
        artifact = artifact_mem.MemArtifact()

    mapper = mappers_python.map_to_python(
        wb_type, artifact, mapper_options={"use_stable_refs": False}
    )
    return mapper.apply(obj)
