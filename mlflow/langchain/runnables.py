import os
from pathlib import Path
from typing import Union

import cloudpickle
import yaml

from mlflow.exceptions import MlflowException
from mlflow.langchain.utils import (
    _BASE_LOAD_KEY,
    _CONFIG_LOAD_KEY,
    _MODEL_DATA_FOLDER_NAME,
    _MODEL_DATA_KEY,
    _MODEL_DATA_PKL_FILE_NAME,
    _MODEL_DATA_YAML_FILE_NAME,
    _MODEL_LOAD_KEY,
    _MODEL_TYPE_KEY,
    _RUNNABLE_LOAD_KEY,
    _UNSUPPORTED_MODEL_ERROR_MESSAGE,
    _load_base_lcs,
    _load_from_json,
    _load_from_pickle,
    _load_from_yaml,
    _save_base_lcs,
    _validate_and_wrap_lc_model,
    base_lc_types,
    custom_type_to_loader_dict,
    lc_runnable_branch_type,
    lc_runnable_with_steps_types,
    lc_runnables_types,
    picklable_runnable_types,
)

_STEPS_FOLDER_NAME = "steps"
_RUNNABLE_STEPS_FILE_NAME = "steps.yaml"
_BRANCHES_FOLDER_NAME = "branches"
_RUNNABLE_BRANCHES_FILE_NAME = "branches.yaml"
_DEFAULT_BRANCH_NAME = "default"


def _load_model_from_config(path, model_config):
    from langchain.chains.loading import type_to_loader_dict as chains_type_to_loader_dict
    from langchain.llms import get_type_to_cls_dict as llms_get_type_to_cls_dict

    try:
        from langchain.prompts.loading import type_to_loader_dict as prompts_types
    except ImportError:
        prompts_types = {"prompt", "few_shot_prompt"}

    config_path = os.path.join(path, model_config.get(_MODEL_DATA_KEY, _MODEL_DATA_YAML_FILE_NAME))
    # Load runnables from config file
    if config_path.endswith(".yaml"):
        config = _load_from_yaml(config_path)
    elif config_path.endswith(".json"):
        config = _load_from_json(config_path)
    else:
        raise MlflowException(
            f"Cannot load runnable without a config file. Got path {config_path}."
        )
    _type = config.get("_type")
    if _type in chains_type_to_loader_dict:
        from langchain.chains.loading import load_chain

        return load_chain(config_path)
    elif _type in prompts_types:
        from langchain.prompts.loading import load_prompt

        return load_prompt(config_path)
    elif _type in llms_get_type_to_cls_dict():
        from langchain.llms.loading import load_llm

        return load_llm(config_path)
    elif _type in custom_type_to_loader_dict():
        return custom_type_to_loader_dict()[_type](config)
    raise MlflowException(f"Unsupported type {_type} for loading.")


def _load_model_from_path(path: str, model_config=None):
    model_load_fn = model_config.get(_MODEL_LOAD_KEY)
    if model_load_fn == _RUNNABLE_LOAD_KEY:
        return _load_runnables(path, model_config)
    if model_load_fn == _BASE_LOAD_KEY:
        return _load_base_lcs(path, model_config)
    if model_load_fn == _CONFIG_LOAD_KEY:
        return _load_model_from_config(path, model_config)
    raise MlflowException(f"Unsupported model load key {model_load_fn}")


def _load_runnable_with_steps(file_path: Union[Path, str], model_type: str):
    """Load the model

    Args:
        file_path: Path to file to load the model from.
        model_type: Type of the model to load.
    """
    from langchain.schema.runnable import RunnableParallel, RunnableSequence

    # Convert file to Path object.
    load_path = Path(file_path) if isinstance(file_path, str) else file_path
    if not load_path.exists() or not load_path.is_dir():
        raise MlflowException(
            f"File {load_path} must exist and must be a directory "
            "in order to load runnable with steps."
        )

    steps_conf_file = load_path / _RUNNABLE_STEPS_FILE_NAME
    if not steps_conf_file.exists():
        raise MlflowException(
            f"File {steps_conf_file} must exist in order to load runnable with steps."
        )
    steps_conf = _load_from_yaml(steps_conf_file)
    steps_path = load_path / _STEPS_FOLDER_NAME
    if not steps_path.exists() or not steps_path.is_dir():
        raise MlflowException(
            f"Folder {steps_path} must exist and must be a directory "
            "in order to load runnable with steps."
        )

    steps = {}
    # ignore hidden files
    for step in (f for f in os.listdir(steps_path) if not f.startswith(".")):
        config = steps_conf.get(step)
        # load model from the folder of the step
        runnable = _load_model_from_path(os.path.join(steps_path, step), config)
        steps[step] = runnable

    if model_type == RunnableSequence.__name__:
        steps = [value for _, value in sorted(steps.items(), key=lambda item: int(item[0]))]
        return runnable_sequence_from_steps(steps)
    if model_type == RunnableParallel.__name__:
        return RunnableParallel(steps)


def runnable_sequence_from_steps(steps):
    """Construct a RunnableSequence from steps.

    Args:
        steps: List of steps to construct the RunnableSequence from.
    """
    from langchain.schema.runnable import RunnableSequence

    if len(steps) < 2:
        raise ValueError(f"RunnableSequence must have at least 2 steps, got {len(steps)}.")

    first, *middle, last = steps
    return RunnableSequence(first=first, middle=middle, last=last)


def _load_runnable_branch(file_path: Union[Path, str], model_type: str):
    """Load the model

    Args:
        file_path: Path to file to load the model from.
        model_type: Type of the model to load.
    """
    from langchain.schema.runnable import RunnableBranch

    # Convert file to Path object.
    load_path = Path(file_path) if isinstance(file_path, str) else file_path
    if not load_path.exists() or not load_path.is_dir():
        raise MlflowException(
            f"File {load_path} must exist and must be a directory "
            "in order to load runnable with steps."
        )

    branches_conf_file = load_path / _RUNNABLE_BRANCHES_FILE_NAME
    if not branches_conf_file.exists():
        raise MlflowException(
            f"File {branches_conf_file} must exist in order to load runnable with steps."
        )
    branches_conf = _load_from_yaml(branches_conf_file)
    branches_path = load_path / _BRANCHES_FOLDER_NAME
    if not branches_path.exists() or not branches_path.is_dir():
        raise MlflowException(
            f"Folder {branches_path} must exist and must be a directory "
            "in order to load runnable with steps."
        )

    branches = []
    for branch in os.listdir(branches_path):
        # load model from the folder of the branch
        if branch == _DEFAULT_BRANCH_NAME:
            default_branch_path = branches_path / _DEFAULT_BRANCH_NAME
            default = _load_model_from_path(
                default_branch_path, branches_conf.get(_DEFAULT_BRANCH_NAME)
            )
        else:
            branch_tuple = []
            for i in range(2):
                config = branches_conf.get(f"{branch}-{i}")
                runnable = _load_model_from_path(
                    os.path.join(branches_path, branch, str(i)), config
                )
                branch_tuple.append(runnable)
            branches.append(tuple(branch_tuple))

    # default branch must be the last branch
    branches.append(default)

    return RunnableBranch(*branches)


def _save_internal_runnables(runnable, path, loader_fn, persist_dir):
    conf = {}
    if isinstance(runnable, lc_runnables_types()):
        conf[_MODEL_TYPE_KEY] = runnable.__class__.__name__
        conf.update(_save_runnables(runnable, path, loader_fn, persist_dir))
    elif isinstance(runnable, lc_runnable_branch_type()):
        conf[_MODEL_TYPE_KEY] = runnable.__class__.__name__
        conf.update(_save_runnable_branch(runnable, path, loader_fn, persist_dir))
    elif isinstance(runnable, base_lc_types()):
        lc_model = _validate_and_wrap_lc_model(runnable, loader_fn)
        conf[_MODEL_TYPE_KEY] = lc_model.__class__.__name__
        conf.update(_save_base_lcs(lc_model, path, loader_fn, persist_dir))
    else:
        conf = {
            _MODEL_TYPE_KEY: runnable.__class__.__name__,
            _MODEL_DATA_KEY: _MODEL_DATA_YAML_FILE_NAME,
            _MODEL_LOAD_KEY: _CONFIG_LOAD_KEY,
        }
        path = path / _MODEL_DATA_YAML_FILE_NAME
        # Save some simple runnables that langchain natively supports.
        if hasattr(runnable, "save"):
            runnable.save(path)
        # TODO: check if `dict` is enough to load it back
        elif hasattr(runnable, "dict"):
            runnable_dict = runnable.dict()
            with open(path, "w") as f:
                yaml.dump(runnable_dict, f, default_flow_style=False)
        else:
            return
    return conf


def _save_runnable_with_steps(steps, file_path: Union[Path, str], loader_fn=None, persist_dir=None):
    """Save the model with steps. Currently it supports saving RunnableSequence and
    RunnableParallel.

    If saving a RunnableSequence, steps is a list of Runnable objects. We save each step to the
    subfolder named by the step index.
    e.g.  - model
            - steps
              - 0
                - model.yaml
              - 1
                - model.pkl
            - steps.yaml
    If saving a RunnableParallel, steps is a dictionary of key-Runnable pairs. We save each step to
    the subfolder named by the key.
    e.g.  - model
            - steps
              - context
                - model.yaml
              - question
                - model.pkl
            - steps.yaml

    We save steps.yaml file to the model folder. It contains each step's model's configuration.

    Args:
        steps: Steps of the runnable.
        file_path: Path to file to save the model to.
    """
    # Convert file to Path object.
    save_path = Path(file_path) if isinstance(file_path, str) else file_path
    save_path.mkdir(parents=True, exist_ok=True)

    # Save steps into a folder
    steps_path = save_path / _STEPS_FOLDER_NAME
    steps_path.mkdir()

    if isinstance(steps, list):
        generator = enumerate(steps)
    elif isinstance(steps, dict):
        generator = steps.items()
    unsaved_runnables = {}
    steps_conf = {}
    for key, runnable in generator:
        step = str(key)
        # Save each step into a subfolder named by step
        save_runnable_path = steps_path / step
        save_runnable_path.mkdir()
        if result := _save_internal_runnables(runnable, save_runnable_path, loader_fn, persist_dir):
            steps_conf[step] = result
        else:
            unsaved_runnables[step] = str(runnable)

    if unsaved_runnables:
        raise MlflowException(
            f"Failed to save runnable sequence: {unsaved_runnables}. "
            "Runnable must have either `save` or `dict` method."
        )

    # save steps configs
    with save_path.joinpath(_RUNNABLE_STEPS_FILE_NAME).open("w") as f:
        yaml.dump(steps_conf, f, default_flow_style=False)


def _save_runnable_branch(model, file_path, loader_fn, persist_dir):
    """
    Save runnable branch in to path.
    """
    save_path = Path(file_path) if isinstance(file_path, str) else file_path
    save_path.mkdir(parents=True, exist_ok=True)
    # save branches into a folder
    branches_path = save_path / _BRANCHES_FOLDER_NAME
    branches_path.mkdir()

    unsaved_runnables = {}
    branches_conf = {}
    for index, branch_tuple in enumerate(model.branches):
        # Save each branch into a subfolder named by index
        # and save condition and runnable into subfolder
        for i, runnable in enumerate(branch_tuple):
            save_runnable_path = branches_path / str(index) / str(i)
            save_runnable_path.mkdir(parents=True)
            branches_conf[f"{index}-{i}"] = {}

            if result := _save_internal_runnables(
                runnable, save_runnable_path, loader_fn, persist_dir
            ):
                branches_conf[f"{index}-{i}"] = result
            else:
                unsaved_runnables[f"{index}-{i}"] = str(runnable)

    # save default branch
    default_branch_path = branches_path / _DEFAULT_BRANCH_NAME
    default_branch_path.mkdir()
    if result := _save_internal_runnables(
        model.default, default_branch_path, loader_fn, persist_dir
    ):
        branches_conf[_DEFAULT_BRANCH_NAME] = result
    else:
        unsaved_runnables[_DEFAULT_BRANCH_NAME] = str(model.default)

    if unsaved_runnables:
        raise MlflowException(
            f"Failed to save runnable branch: {unsaved_runnables}. "
            "Runnable must have either `save` or `dict` method."
        )

    # save branches configs
    with save_path.joinpath(_RUNNABLE_BRANCHES_FILE_NAME).open("w") as f:
        yaml.dump(branches_conf, f, default_flow_style=False)


def _save_picklable_runnable(model, path):
    if not path.endswith(".pkl"):
        raise ValueError(f"File path must end with .pkl, got {path}.")
    with open(path, "wb") as f:
        cloudpickle.dump(model, f)


def _save_runnables(model, path, loader_fn=None, persist_dir=None):
    model_data_kwargs = {_MODEL_LOAD_KEY: _RUNNABLE_LOAD_KEY}
    if isinstance(model, lc_runnable_with_steps_types()):
        model_data_path = _MODEL_DATA_FOLDER_NAME
        _save_runnable_with_steps(
            model.steps, os.path.join(path, model_data_path), loader_fn, persist_dir
        )
    elif isinstance(model, picklable_runnable_types()):
        model_data_path = _MODEL_DATA_PKL_FILE_NAME
        _save_picklable_runnable(model, os.path.join(path, model_data_path))
    elif isinstance(model, lc_runnable_branch_type()):
        model_data_path = _MODEL_DATA_FOLDER_NAME
        _save_runnable_branch(model, os.path.join(path, model_data_path), loader_fn, persist_dir)
    else:
        raise MlflowException.invalid_parameter_value(
            _UNSUPPORTED_MODEL_ERROR_MESSAGE.format(instance_type=type(model).__name__)
        )
    model_data_kwargs.update({_MODEL_DATA_KEY: model_data_path})
    return model_data_kwargs


def _load_runnables(path, conf):
    model_type = conf.get(_MODEL_TYPE_KEY)
    model_data = conf.get(_MODEL_DATA_KEY, _MODEL_DATA_YAML_FILE_NAME)
    if model_type in (x.__name__ for x in lc_runnable_with_steps_types()):
        return _load_runnable_with_steps(os.path.join(path, model_data), model_type)
    if (
        model_type in (x.__name__ for x in picklable_runnable_types())
        or model_data == _MODEL_DATA_PKL_FILE_NAME
    ):
        return _load_from_pickle(os.path.join(path, model_data))
    if model_type in (x.__name__ for x in lc_runnable_branch_type()):
        return _load_runnable_branch(os.path.join(path, model_data), model_type)
    raise MlflowException.invalid_parameter_value(
        _UNSUPPORTED_MODEL_ERROR_MESSAGE.format(instance_type=model_type)
    )
