# Copyright (C) 2019-2021 Intel Corporation
#
# SPDX-License-Identifier: MIT

import json
import logging as log
import os
import os.path as osp
import shutil
import unittest.mock
import urllib.parse
from contextlib import ExitStack, contextmanager
from enum import Enum
from glob import glob
from io import IOBase
from typing import Dict, List, Optional, Tuple, Union

from ruamel.yaml import YAML
import networkx as nx

from datumaro.components.config import Config
from datumaro.components.config_model import (BuildStage, PipelineConfig, ProjectLayout,
    Remote, Source, TreeConfig, TreeLayout)
from datumaro.components.dataset import DEFAULT_FORMAT, Dataset, IDataset
from datumaro.components.environment import Environment
from datumaro.components.errors import (DatasetMergeError, DatumaroError,
    DetachedProjectError, EmptyPipelineError, MissingObjectError,
                                        MissingPipelineHeadError,
                                        MultiplePipelineHeadsError,
                                        ProjectAlreadyExists,
                                        ProjectNotFoundError,
                                        ReadonlyProjectError,
                                        SourceExistsError, UnknownRefError,
                                        UnknownSourceError, UnknownStageError,
                                        VcsError)
from datumaro.util import (error_rollback, find, parse_str_enum_value,
                           str_to_bool)
from datumaro.util.log_utils import catch_logs, logging_disabled
from datumaro.util.os_util import generate_next_name, make_file_name, rmtree


class ProjectSourceDataset(Dataset):
    @classmethod
    def from_cache(cls, tree: 'Tree', source: str, path: str):
        config = tree.sources[source]

        dataset = cls.import_from(path, env=tree.env,
            format=config.format, **config.options)
        dataset._tree = tree
        dataset._config = config
        dataset._readonly = True
        dataset.name = source
        return dataset

    @classmethod
    def from_source(cls, tree: 'Tree', source: str):
        config = tree.sources[source]

        path = osp.join(tree.sources.data_dir(source), config.url)
        readonly = not path or not osp.exists(path)
        if path and not osp.exists(path) and not config.remote:
            # backward compatibility
            path = osp.join(tree.config.project_dir, config.url)
            readonly = True

        dataset = cls.import_from(path, env=tree.env,
            format=config.format, **config.options)
        dataset._tree = tree
        dataset._config = config
        dataset._readonly = readonly
        dataset.name = source
        return dataset

    def save(self, save_dir=None, **kwargs):
        if save_dir is None:
            if self.readonly:
                raise ReadonlyProjectError("Can't update a read-only dataset")
        super().save(save_dir, **kwargs)

    @property
    def readonly(self):
        return not self._readonly and self.is_bound

    @property
    def _env(self):
        return self._tree.env

    @property
    def config(self):
        return self._config

    def run_model(self, model, batch_size=1):
        if isinstance(model, str):
            model = self._tree.models.make_executable_model(model)
        return super().run_model(model, batch_size=batch_size)


MergeStrategy = Enum('MergeStrategy', ['ours', 'theirs', 'conflict'])

class CrudProxy:
    @property
    def _data(self):
        raise NotImplementedError()

    def __len__(self):
        return len(self._data)

    def __getitem__(self, name):
        return self._data[name]

    def get(self, name, default=None):
        return self._data.get(name, default)

    def __iter__(self):
        return iter(self._data.keys())

    def items(self):
        return iter(self._data.items())

    def __contains__(self, name):
        return name in self._data

class ProjectRepositories(CrudProxy):
    def __init__(self, project_vcs):
        self._vcs = project_vcs

    def set_default(self, name):
        if name not in self:
            raise KeyError("Unknown repository name '%s'" % name)
        self._vcs._project.config.default_repo = name

    def get_default(self):
        return self._vcs._project.config.default_repo

    @CrudProxy._data.getter
    def _data(self):
        return self._vcs.git.list_remotes()

    def add(self, name, url):
        self._vcs.git.add_remote(name, url)

    def remove(self, name):
        self._vcs.git.remove_remote(name)

class ProjectRemotes(CrudProxy):
    SUPPORTED_PROTOCOLS = {'', 'remote', 's3', 'ssh', 'http', 'https'}

    def __init__(self, project_vcs):
        self._vcs = project_vcs

    def fetch(self, name=None):
        self._vcs.dvc.fetch_remote(name)

    def pull(self, name=None):
        self._vcs.dvc.pull_remote(name)

    def push(self, name=None):
        self._vcs.dvc.push_remote(name)

    def set_default(self, name):
        self._vcs.dvc.set_default_remote(name)

    def get_default(self):
        return self._vcs.dvc.get_default_remote()

    @CrudProxy._data.getter
    def _data(self):
        return self._vcs._project.config.remotes

    def add(self, name, value):
        url_parts = self.validate_url(value['url'])
        if not url_parts.scheme:
            value['url'] = osp.abspath(value['url'])
        if not value.get('type'):
            value['type'] = 'url'

        if not isinstance(value, Remote):
            value = Remote(value)
        value = self._data.set(name, value)

        assert value.type in {'url', 'git', 'dvc'}, value.type
        self._vcs.dvc.add_remote(name, value)
        return value

    def remove(self, name, force=False):
        try:
            self._vcs.dvc.remove_remote(name)
        except DvcWrapper.DvcError:
            if not force:
                raise

    @classmethod
    def validate_url(cls, url):
        url_parts = urllib.parse.urlsplit(url)
        if url_parts.scheme not in cls.SUPPORTED_PROTOCOLS and \
                not osp.exists(url):
            if url_parts.scheme == 'git':
                raise ValueError("git sources should be added as remote links")
            if url_parts.scheme == 'dvc':
                raise ValueError("dvc sources should be added as remote links")
            raise ValueError(
                "Invalid remote '%s': scheme '%s' is not supported, the only"
                "available are: %s" %
                (url, url_parts.scheme, ', '.join(cls.SUPPORTED_PROTOCOLS))
            )
        if not (url_parts.hostname or url_parts.path):
            raise ValueError("URL must not be empty, url: '%s'" % url)
        return url_parts

class _DataSourceBase(CrudProxy):
    def __init__(self, project, config_field):
        self._project = project
        self._field = config_field

    @CrudProxy._data.getter
    def _data(self):
        return self._project.config[self._field]

    def pull(self, names=None, rev=None):
        if not self._project.vcs.writeable:
            raise ReadonlyProjectError("Can't pull in a read-only project")

        if not names:
            names = []
        elif isinstance(names, str):
            names = [names]
        else:
            names = list(names)

        for name in names:
            if name and name not in self:
                raise KeyError("Unknown source '%s'" % name)

        if rev and len(names) != 1:
            raise ValueError("A revision can only be specified for a "
                "single source invocation")

        self._project.vcs.dvc.update_imports(
            [self.dvcfile_path(name) for name in names], rev=rev)

    @classmethod
    def _validate_url(cls, url):
        return ProjectRemotes.validate_url(url)

    @classmethod
    def _make_remote_name(cls, name):
        return name

    def data_dir(self, name: str) -> str:
        return osp.join(self._project.config.project_dir, name)

    def validate_name(self, name: str):
        valid_filename = make_file_name(name)
        if valid_filename != name:
            raise ValueError("Source name contains "
                "prohibited symbols: %s" % (set(name) - set(valid_filename)) )

        if name.startswith('.'):
            raise ValueError("Source name can't start with '.'")

    def dvcfile_path(self, name):
        return self._project.vcs.dvc_filepath(name)

    @classmethod
    def _fix_dvc_file(cls, source_path, dvc_path, dst_name):
        with open(dvc_path, 'r+') as dvc_file:
            yaml = YAML(typ='rt')
            dvc_data = yaml.load(dvc_file)
            dvc_data['wdir'] = osp.join(
                dvc_data['wdir'], osp.basename(source_path))
            dvc_data['outs'][0]['path'] = dst_name

            dvc_file.seek(0)
            yaml.dump(dvc_data, dvc_file)
            dvc_file.truncate()

    def _ensure_in_dir(self, source_path, dvc_path, dst_name):
        if not osp.isfile(source_path):
            return
        tmp_dir = osp.join(self._project.config.project_dir,
            self._project.config.env_dir, 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        source_tmp = osp.join(tmp_dir, osp.basename(source_path))
        os.replace(source_path, source_tmp)
        os.makedirs(source_path)
        os.replace(source_tmp, osp.join(source_path, dst_name))

        self._fix_dvc_file(source_path, dvc_path, dst_name)

    @error_rollback('on_error', implicit=True)
    def add(self, name, value):
        self.validate_name(name)

        if name in self:
            raise SourceExistsError("Source '%s' already exists" % name)

        url = value.get('url', '')

        if self._project.vcs.writeable:
            if url:
                url_parts = self._validate_url(url)

            if not url:
                # a generated source
                remote_name = ''
                path = url
            elif url_parts.scheme == 'remote':
                # add a source with existing remote
                remote_name = url_parts.netloc
                remote_conf = self._project.vcs.remotes[remote_name]
                path = url_parts.path
                if path == '/': # fix conflicts in remote interpretation
                    path = ''
                url = remote_conf.url + path
            else:
                # add a source and a new remote
                if not url_parts.scheme and not osp.exists(url):
                    raise FileNotFoundError(
                        "Can't find file or directory '%s'" % url)

                remote_name = self._make_remote_name(name)
                if remote_name not in self._project.vcs.remotes:
                    on_error.do(self._project.vcs.remotes.remove, remote_name,
                        ignore_errors=True)
                remote_conf = self._project.vcs.remotes.add(remote_name, {
                    'url': url,
                    'type': 'url',
                })
                path = ''

            source_dir = self.data_dir(name)

            dvcfile = self.dvcfile_path(name)
            if not osp.isfile(dvcfile):
                on_error.do(os.remove, dvcfile, ignore_errors=True)

            if not remote_name:
                pass
            elif remote_conf.type == 'url':
                self._project.vcs.dvc.import_url(
                    'remote://%s%s' % (remote_name, path),
                    out=source_dir, dvc_path=dvcfile, download=True)
                self._ensure_in_dir(source_dir, dvcfile, osp.basename(url))
            elif remote_conf.type in {'git', 'dvc'}:
                self._project.vcs.dvc.import_repo(remote_conf.url, path=path,
                    out=source_dir, dvc_path=dvcfile, download=True)
                self._ensure_in_dir(source_dir, dvcfile, osp.basename(url))
            else:
                raise ValueError("Unknown remote type '%s'" % remote_conf.type)

            path = osp.basename(path)
        else:
            if not url or osp.exists(url):
                # a local or a generated source
                # in a read-only or in-memory project
                remote_name = ''
                path = url
            else:
                raise DetachedProjectError(
                    "Can only add an existing local, or generated "
                    "source to a detached project")

        value['url'] = path
        value['remote'] = remote_name
        value = self._data.set(name, value)

        return value

    def remove(self, name, force=False, keep_data=True):
        """Force - ignores errors and tries to wipe remaining data"""

        if name not in self._data and not force:
            raise KeyError("Unknown source '%s'" % name)

        self._data.remove(name)

        if not self._project.vcs.writeable:
            return

        if force and not keep_data:
            source_dir = self.data_dir(name)
            if osp.isdir(source_dir):
                shutil.rmtree(source_dir, ignore_errors=True)

        dvcfile = self.dvcfile_path(name)
        if osp.isfile(dvcfile):
            try:
                self._project.vcs.dvc.remove(dvcfile, outs=not keep_data)
            except DvcWrapper.DvcError:
                if force:
                    os.remove(dvcfile)
                else:
                    raise

        self._project.vcs.remotes.remove(name, force=force)

class ProjectModels(_DataSourceBase):
    def __init__(self, project):
        super().__init__(project, 'models')

    def __getitem__(self, name):
        try:
            return super().__getitem__(name)
        except KeyError:
            raise KeyError("Unknown model '%s'" % name)

    def work_dir(self, name):
        return osp.join(
            self._project.config.project_dir,
            self._project.config.env_dir,
            self._project.config.models_dir, name)

    def make_executable_model(self, name):
        model = self[name]
        return self._project.env.make_launcher(model.launcher,
            **model.options, model_dir=self.work_dir(name))

class ProjectSources(_DataSourceBase):
    def __init__(self, project):
        super().__init__(project, 'sources')

    def __getitem__(self, name):
        try:
            return super().__getitem__(name)
        except KeyError:
            raise KeyError("Unknown source '%s'" % name)

    def make_dataset(self, name, rev=None):
        return ProjectSourceDataset.from_source(self._project, name)

    def validate_name(self, name):
        super().validate_name(name)

        reserved_names = {'dataset', 'build', 'project'}
        if name.lower() in reserved_names:
            raise ValueError("Source name is reserved for internal use")

    def add(self, name, value):
        value = super().add(name, value)

        self._project.build_targets.add_target(name)

        return value

    def remove(self, name, force=False, keep_data=True):
        self._project.build_targets.remove_target(name)

        super().remove(name, force=force, keep_data=keep_data)


BuildStageType = Enum('BuildStageType',
    ['source', 'project', 'transform', 'filter', 'convert', 'inference'])

class Pipeline:
    @staticmethod
    def _create_graph(config: PipelineConfig):
        graph = nx.DiGraph()
        for entry in config:
            target_name = entry['name']
            parents = entry['parents']
            target = BuildStage(entry['config'])

            graph.add_node(target_name, config=target)
            for prev_stage in parents:
                graph.add_edge(prev_stage, target_name)

        return graph

    def __init__(self, config: PipelineConfig = None):
        self._head = None

        if config is not None:
            self._graph = self._create_craph(config)
            if not self.head:
                raise MissingPipelineHeadError()
        else:
            self._graph = nx.DiGraph()

    def __getattr__(self, key):
        notfound = object()
        obj = getattr(self._graph, key, notfound)
        if obj is notfound:
            raise AttributeError(key)
        return obj

    @staticmethod
    def _find_head_node(graph) -> str:
        head = None
        for node in graph.nodes:
            if graph.out_degree(node) == 0:
                if head is not None:
                    raise MultiplePipelineHeadsError(
                        "A pipeline can have only one " \
                        "main target, but it has at least 2: %s, %s" % \
                        (head, node))
                head = node
        return head

    @property
    def head(self):
        if self._head is None:
            self._head = self._find_head_node(self._graph)
        return self._graph[self._head]

    @staticmethod
    def _serialize(graph) -> PipelineConfig:
        serialized = PipelineConfig()
        for node_name, node in graph.nodes.items():
            serialized.nodes.append({
                'name': node_name,
                'parents': list(graph.predecessors(node_name)),
                'config': dict(node['config']),
            })
        return serialized

    @staticmethod
    def _get_subgraph(graph, target):
        target_parents = set()
        visited = set()
        to_visit = {target}
        while to_visit:
            current = to_visit.pop()
            visited.add(current)
            for pred in graph.predecessors(current):
                target_parents.add(pred)
                if pred not in visited:
                    to_visit.add(pred)

        target_parents.add(target)

        return graph.subgraph(target_parents)

    def get_slice(self, target) -> 'Pipeline':
        pipeline = Pipeline()
        pipeline._graph = self._get_subgraph(self._graph, target)
        return pipeline

class ProjectBuilder:
    def __init__(self, project: 'Project', tree: 'Tree'):
        self._project = project
        self._tree = tree

    def make_dataset(self, pipeline) -> IDataset:
        dataset = self._get_resulting_dataset(pipeline)

        # need to save and load, because it can modify dataset,
        # unless we work with the internal format
        # save_in_cache(project, pipeline) # update and check hash in config!
        # dataset = load_dataset(project, pipeline)

        return dataset

    def _run_pipeline(self, pipeline):
        missing_sources = self.find_missing_sources(pipeline)
        for t in missing_sources:
            self._project.download_source(pipeline.nodes[t]['config'])

        return self._init_pipeline(pipeline)

    def _get_resulting_dataset(self, pipeline):
        graph, head = self._run_pipeline(pipeline)
        return graph[head]['dataset']

    def _init_pipeline(self, pipeline):
        def _load_cached_dataset(stage_config, stage_name):
            path = self._project._make_cache_path(stage_config.hash)
            source = ProjectBuildTargets.strip_target_name(stage_name)
            return ProjectSourceDataset.from_cache(self._tree, source, path)

        def _join_parent_datasets(force=True):
            parents = { p: graph.nodes[p] for p in initialized_parents }

            if 1 < len(parents) or force:
                try:
                    dataset = Dataset.from_extractors(
                        *(p['dataset'] for p in parents.values()),
                        env=self._project.env)
                except DatasetMergeError as e:
                    e.sources = set(parents)
                    raise e
            else:
                dataset = parents[0]

            # clear fully utilized datasets to release memory
            for p_name, p in parents.items():
                p['_use_count'] = p.get('_use_count', 0) + 1

                if p_name != head and \
                        p['_use_count'] == len(graph.successors(p_name)):
                    p.pop('dataset')

            return dataset

        if len(pipeline) == 0:
            raise EmptyPipelineError()

        graph = pipeline._graph

        head = pipeline.head
        if not head:
            raise MissingPipelineHeadError()
        head = head['config'].name

        # traverse the graph and initialize nodes from sources to the head
        to_visit = [head]
        while to_visit:
            current_name = to_visit.pop()
            current = graph.nodes[current_name]

            assert current.get('dataset') is None

            obj_hash = current['config'].hash
            if obj_hash and self._project.is_obj_cached(obj_hash):
                current['dataset'] = _load_cached_dataset(current['config'],
                    current_name)
                continue

            uninitialized_parents = []
            initialized_parents = []
            parent_targets = graph.predecessors(current_name)
            if not parent_targets:
                assert current['config'].type == 'source', current['config'].type
                if not current['config'].is_generated:
                    # source is missing in the cache and cannot be retrieved
                    # it is assumed that all the sources were downloaded earlier
                    raise MissingObjectError(obj_hash)
            else:
                for p_name in parent_targets:
                    parent = graph.nodes[p_name]
                    if parent.get('dataset') is None:
                        uninitialized_parents.append(p_name)
                    else:
                        initialized_parents.append(p_name)

                if uninitialized_parents:
                    to_visit.append(current_name)
                    to_visit.extend(uninitialized_parents)
                    continue

            type_ = BuildStageType[current['config'].type]
            params = current['config'].params
            if type_ == BuildStageType.transform:
                kind = current['config'].kind
                try:
                    transform = self._tree.env.transforms[kind]
                except KeyError:
                    raise UnknownStageError("Unknown transform '%s'" % kind)

                dataset = _join_parent_datasets()
                dataset = dataset.transform(transform, **params)

            elif type_ == BuildStageType.filter:
                dataset = _join_parent_datasets()
                dataset = dataset.filter(**params)

            elif type_ == BuildStageType.inference:
                kind = current['config'].kind
                model = self._tree.models.make_executable_model(kind)

                dataset = _join_parent_datasets()
                dataset = dataset.run_model(model)

            elif type_ == BuildStageType.source:
                assert len(initialized_parents) == 0, current_name
                source = ProjectBuildTargets.strip_target_name(current_name)
                dataset = self._tree.sources.make_dataset(source)

            elif type_ == BuildStageType.project:
                dataset = _join_parent_datasets(force=True)

            elif type_ == BuildStageType.convert:
                dataset = _join_parent_datasets()

            else:
                raise UnknownStageError("Unknown stage type '%s'" % type_)

            current['dataset'] = dataset

        return graph, head

    def find_missing_sources(self, pipeline: Pipeline):
        missing_sources = set()
        checked_deps = set()
        missing_deps = [pipeline.head['config'].name]
        while missing_deps:
            t = missing_deps.pop()
            if t in checked_deps:
                continue

            t_conf = pipeline.nodes[t]['config']

            obj_hash = t_conf.hash
            if not (obj_hash and self._project.is_obj_cached(obj_hash)):
                parent_targets = pipeline.parents(t)
                if not parent_targets:
                    assert t_conf.type == 'source', t_conf.type
                    if not t_conf.is_generated:
                        missing_sources.add(t)
                else:
                    for p in parent_targets:
                        if p not in checked_deps:
                            missing_deps.append(p)
                    continue

            checked_deps.add(t)
        return missing_sources

class ProjectBuildTargets(CrudProxy):
    MAIN_TARGET = 'project'
    BASE_STAGE = 'root'

    def __init__(self, project):
        self._project = project

    @CrudProxy._data.getter
    def _data(self):
        data = self._project.config.build_targets

        if self.MAIN_TARGET not in data:
            data[self.MAIN_TARGET] = {
                'stages': [
                    BuildStage({
                        'name': self.BASE_STAGE,
                        'type': BuildStageType.project.name,
                    }),
                ]
            }

        for source in self._project.sources:
            if source not in data:
                data[source] = {
                    'stages': [
                        BuildStage({
                            'name': self.BASE_STAGE,
                            'type': BuildStageType.source.name,
                        }),
                    ]
                }

        return data

    def __contains__(self, key):
        if '.' in key:
            target, stage = self.split_target_name(key)
            return target in self._data and \
                self._data[target].find_stage(stage) is not None
        return key in self._data

    def add_target(self, name):
        return self._data.set(name, {
            'stages': [
                BuildStage({
                    'name': self.BASE_STAGE,
                    'type': BuildStageType.source.name,
                }),
            ]
        })

    def add_stage(self, target, value, prev=None,
            name=None) -> Tuple[BuildStage, str]:
        target_name = target
        target_stage_name = None
        if '.' in target:
            target_name, target_stage_name = self.split_target_name(target)

        if prev is None:
            prev = target_stage_name

        target = self._data[target_name]

        if prev:
            prev_stage = find(enumerate(target.stages),
                lambda e: e[1].name == prev)
            if prev_stage is None:
                raise KeyError("Can't find stage '%s'" % prev)
            prev_stage = prev_stage[0]
        else:
            prev_stage = len(target.stages) - 1

        name = value.get('name') or name
        if not name:
            name = generate_next_name((s.name for s in target.stages),
                value['type'], sep='-')
        else:
            if target.find_stage(name):
                raise VcsError("Stage '%s' already exists" % name)
        value['name'] = name

        value = BuildStage(value)
        assert BuildStageType[value.type]
        target.stages.insert(prev_stage + 1, value)
        return value, self.make_target_name(target_name, name)

    def remove_target(self, name):
        assert name != self.MAIN_TARGET, "Can't remove the main target"
        self._data.remove(name)

    def remove_stage(self, target, name):
        assert name not in {self.BASE_STAGE}, "Can't remove a default stage"

        target = self._data[target]
        idx = find(enumerate(target.stages), lambda e: e[1].name == name)
        if idx is None:
            raise KeyError("Can't find stage '%s'" % name)
        target.stages.remove(idx)

    def add_transform_stage(self, target, transform, params=None, name=None):
        if not transform in self._project.env.transforms:
            raise KeyError("Unknown transform '%s'" % transform)

        return self.add_stage(target, {
            'type': BuildStageType.transform.name,
            'kind': transform,
            'params': params or {},
        }, name=name)

    def add_inference_stage(self, target, model, name=None):
        if not model in self._project.config.models:
            raise KeyError("Unknown model '%s'" % model)

        return self.add_stage(target, {
            'type': BuildStageType.inference.name,
            'kind': model,
        }, name=name)

    def add_filter_stage(self, target, params=None, name=None):
        return self.add_stage(target, {
            'type': BuildStageType.filter.name,
            'params': params or {},
        }, name=name)

    def add_convert_stage(self, target, format, \
            params=None, name=None): # pylint: disable=redefined-builtin
        if not self._project.env.is_format_known(format):
            raise KeyError("Unknown format '%s'" % format)

        return self.add_stage(target, {
            'type': BuildStageType.convert.name,
            'kind': format,
            'params': params or {},
        }, name=name)

    @staticmethod
    def make_target_name(target, stage=None):
        if stage:
            return '%s.%s' % (target, stage)
        return target

    @classmethod
    def split_target_name(cls, name):
        if '.' in name:
            target, stage = name.split('.', maxsplit=1)
            if not target:
                raise ValueError("Wrong build target name '%s': "
                    "a name can't be empty" % name)
            if not stage:
                raise ValueError("Wrong build target name '%s': "
                    "expected stage name after the separator" % name)
        else:
            target = name
            stage = cls.BASE_STAGE
        return target, stage

    @classmethod
    def strip_target_name(cls, name: str):
        return cls.split_target_name(name)[0]

    def _make_full_pipeline(self) -> Pipeline:
        pipeline = Pipeline()
        graph = pipeline._graph
        for target_name, target in self.items():
            if target_name == self.MAIN_TARGET:
                # main target combines all the others
                prev_stages = [self.make_target_name(n, t.head.name)
                    for n, t in self.items() if n != self.MAIN_TARGET]
            else:
                prev_stages = [self.make_target_name(t, self[t].head.name)
                    for t in target.parents]

            for stage in target.stages:
                stage_name = self.make_target_name(target_name, stage['name'])
                graph.add_node(stage_name, config=stage)
                for prev_stage in prev_stages:
                    graph.add_edge(prev_stage, stage_name)
                prev_stages = [stage_name]

        return pipeline

    def make_pipeline(self, target) -> Pipeline:
        # a subgraph with all the target dependencies
        if '.' not in target:
            target = self.make_target_name(target, self[target].head.name)

        return self._make_full_pipeline().get_slice(target)

    def make_dataset(self, target=None) -> IDataset:
        pipeline = self.make_pipeline(target)
        return ProjectBuilder(self._project, self).make_dataset(pipeline)


class GitWrapper:
    @staticmethod
    def import_module():
        import git
        return git

    try:
        module = import_module.__func__()
    except ImportError:
        module = None

    def _git_dir(self):
        return osp.join(self._project_dir, '.git')

    def __init__(self, project_dir, repo=None):
        self._project_dir = project_dir
        self.repo = repo

        if repo is None and \
                osp.isdir(project_dir) and osp.isdir(self._git_dir()):
            self.repo = self.module.Repo(project_dir)

    @property
    def initialized(self):
        return self.repo is not None

    def init(self):
        if self.initialized:
            return

        repo = self.module.Repo.init(path=self._project_dir)
        repo.config_writer() \
            .set_value("user", "name", "User") \
            .set_value("user", "email", "<>") \
            .release()
        # gitpython does not support init, use git directly
        repo.git.init()

        self.repo = repo

    @property
    def refs(self) -> List[str]:
        return [t.name for t in self.repo.refs]

    @property
    def tags(self) -> List[str]:
        return [t.name for t in self.repo.tags]

    def push(self, remote=None):
        args = [remote] if remote else []
        remote = self.repo.remote(*args)
        branch = self.repo.head.ref.name
        if not self.repo.head.ref.tracking_branch():
            self.repo.git.push('--set-upstream', remote, branch)
        else:
            remote.push(branch)

    def pull(self, remote=None):
        args = [remote] if remote else []
        return self.repo.remote(*args).pull()

    def check_updates(self, remote=None) -> List[str]:
        args = [remote] if remote else []
        remote = self.repo.remote(*args)
        prev_refs = {r.name: r.commit.hexsha for r in remote.refs}
        remote.update()
        new_refs = {r.name: r.commit.hexsha for r in remote.refs}
        updated_refs = [(prev_refs.get(n), new_refs.get(n))
            for n, _ in (set(prev_refs.items()) ^ set(new_refs.items()))]
        return updated_refs

    def fetch(self, remote=None):
        args = [remote] if remote else []
        self.repo.remote(*args).fetch()

    def tag(self, name):
        self.repo.create_tag(name)

    def checkout(self, ref=None, paths=None):
        args = []
        if ref:
            args.append(ref)
        if paths:
            args.append('--')
            args.extend(paths)
        self.repo.git.checkout(*args)

    def add(self, paths, base=None): # pylint: disable=redefined-builtin
        """
        Adds paths to index.
        Paths can be truncated relatively to base.
        """

        kwargs = {}
        if base:
            kwargs['path_rewriter'] = lambda p: osp.relpath(p, base)
        self.repo.index.add(paths, **kwargs)

    def commit(self, message) -> str:
        """
        Creates a new revision from index.
        Returns: new revision hash.
        """
        return self.repo.index.commit(message).hexsha

    def status(self):
        # R[everse] flag is needed for index to HEAD comparison
        # to avoid inversed output in gitpython, which adds this flag
        # git diff --cached HEAD [not not R]
        diff = self.repo.index.diff(R=True)
        return {
            osp.relpath(d.a_rawpath.decode(), self._project_dir): d.change_type
            for d in diff
        }

    def list_remotes(self):
        return { r.name: r.url for r in self.repo.remotes }

    def add_remote(self, name, url):
        self.repo.create_remote(name, url)

    def remove_remote(self, name):
        self.repo.delete_remote(name)

    def is_ref(self, rev):
        try:
            self.repo.commit(rev)
            return True
        except (ValueError, self.module.exc.BadName):
            return False

    def has_commits(self):
        return self.is_ref('HEAD')

    def show(self, path, rev=None):
        return self.repo.git.show('%s:%s' % (rev or '', path))

    def get_tree(self, ref):
        return self.repo.tree(ref)

    def write_tree(self, tree, base_path):
        os.makedirs(base_path, exist_ok=True)

        for obj in tree.traverse(visit_once=True):
            path = osp.join(base_path, obj.path)
            os.makedirs(osp.dirname(path), exist_ok=True)
            if obj.type == 'blob':
                with open(path, 'wb') as f:
                    obj.stream_data(f)
            elif obj.type == 'tree':
                pass
            else:
                raise ValueError("Unexpected object type in a "
                    "git tree: %s (%s)" % (obj.type, obj.hexsha))

    @property
    def head(self) -> str:
        return self.repo.head.hexsha

    def rev_parse(self, ref: str) -> Tuple[str, str]:
        obj = self.repo.rev_parse(ref)
        return obj.type, obj.hexsha

    IgnoreMode = IgnoreMode

    def ignore(self, paths: List[str], mode: Optional[IgnoreMode] = None,
            gitignore: Optional[str] = None):
        if not gitignore:
            gitignore = '.gitignore'
        repo_root = self._project_dir
        gitignore = osp.abspath(osp.join(repo_root, gitignore))
        assert gitignore.startswith(repo_root), gitignore

        _update_ignore_file(paths, repo_root=repo_root,
            mode=mode, filepath=gitignore)

class DvcWrapper:
    @staticmethod
    def import_module():
        import dvc
        import dvc.main
        import dvc.repo
        return dvc

    try:
        module = import_module.__func__()
    except ImportError:
        module = None

    def _dvc_dir(self):
        return osp.join(self._project_dir, '.dvc')

    class DvcError(Exception):
        pass

    def __init__(self, project_dir):
        self._project_dir = project_dir
        self._repo = None

        if osp.isdir(project_dir) and osp.isdir(self._dvc_dir()):
            with logging_disabled():
                self._repo = self.module.repo.Repo(project_dir)

    @property
    def initialized(self):
        return self._repo is not None

    @property
    def repo(self):
        self._repo = self.module.repo.Repo(self._project_dir)
        return self._repo

    def init(self):
        if self.initialized:
            return

        with logging_disabled():
            self._repo = self.module.repo.Repo.init(self._project_dir)

    def push(self, targets=None, remote=None):
        args = ['push']
        if remote:
            args.append('--remote')
            args.append(remote)
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def pull(self, targets=None, remote=None):
        args = ['pull']
        if remote:
            args.append('--remote')
            args.append(remote)
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def check_updates(self, targets=None, remote=None):
        args = ['fetch'] # no other way now?
        if remote:
            args.append('--remote')
            args.append(remote)
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def fetch(self, targets=None, remote=None):
        args = ['fetch']
        if remote:
            args.append('--remote')
            args.append(remote)
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def import_repo(self, url, path, out=None, dvc_path=None, rev=None,
            download=True):
        args = ['import']
        if dvc_path:
            args.append('--file')
            args.append(dvc_path)
            os.makedirs(osp.dirname(dvc_path), exist_ok=True)
        if rev:
            args.append('--rev')
            args.append(rev)
        if out:
            args.append('-o')
            args.append(out)
        if not download:
            args.append('--no-exec')
        args.append(url)
        args.append(path)
        self._exec(args)

    def import_url(self, url, out=None, dvc_path=None, download=True):
        args = ['import-url']
        if dvc_path:
            args.append('--file')
            args.append(dvc_path)
            os.makedirs(osp.dirname(dvc_path), exist_ok=True)
        if not download:
            args.append('--no-exec')
        args.append(url)
        if out:
            args.append(out)
        self._exec(args)

    def update_imports(self, targets=None, rev=None):
        args = ['update']
        if rev:
            args.append('--rev')
            args.append(rev)
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def checkout(self, targets=None):
        args = ['checkout']
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def add(self, paths, dvc_path=None):
        args = ['add']
        if dvc_path:
            args.append('--file')
            args.append(dvc_path)
            os.makedirs(osp.dirname(dvc_path), exist_ok=True)
        if paths:
            if isinstance(paths, str):
                args.append(paths)
            else:
                args.extend(paths)
        self._exec(args)

    def remove(self, paths, outs=False):
        args = ['remove']
        if outs:
            args.append('--outs')
        if paths:
            if isinstance(paths, str):
                args.append(paths)
            else:
                args.extend(paths)
        self._exec(args)

    def commit(self, paths):
        args = ['commit', '--recursive', '--force']
        if paths:
            if isinstance(paths, str):
                args.append(paths)
            else:
                args.extend(paths)
        self._exec(args)

    def add_remote(self, name, config):
        self._exec(['remote', 'add', name, config['url']])

    def remove_remote(self, name):
        self._exec(['remote', 'remove', name])

    def list_remotes(self):
        out = self._exec(['remote', 'list'])
        return dict(line.split() for line in out.split('\n') if line)

    def get_default_remote(self):
        out = self._exec(['remote', 'default'])
        if out == 'No default remote set' or 1 < len(out.split()):
            return None
        return out

    def set_default_remote(self, name):
        assert name and 1 == len(name.split()), "Invalid remote name '%s'" % name
        self._exec(['remote', 'default', name])

    def list_stages(self):
        return set(s.addressing for s in self.repo.stages)

    def run(self, name, cmd, deps=None, outs=None, force=False):
        args = ['run', '-n', name]
        if force:
            args.append('--force')
        for d in deps:
            args.append('-d')
            args.append(d)
        for o in outs:
            args.append('--outs')
            args.append(o)
        args.extend(cmd)
        self._exec(args, hide_output=False)

    def repro(self, targets=None, force=False, pull=False):
        args = ['repro']
        if force:
            args.append('--force')
        if pull:
            args.append('--pull')
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        self._exec(args)

    def status(self, targets=None):
        args = ['status', '--show-json']
        if targets:
            if isinstance(targets, str):
                args.append(targets)
            else:
                args.extend(targets)
        out = self._exec(args).splitlines()[-1]
        return json.loads(out)

    @staticmethod
    def check_stage_status(data, stage, status):
        assert status in {'deleted', 'modified'}
        return status in [s
            for d in data.get(stage, []) if 'changed outs' in d
            for co in d.values()
            for s in co.values()
        ]

    def _exec(self, args, hide_output=True, answer_on_input='y'):
        contexts = ExitStack()

        args = ['--cd', self._project_dir] + args
        contexts.callback(os.chdir, os.getcwd()) # restore cd after DVC

        if answer_on_input is not None:
            def _input(*args): return answer_on_input
            contexts.enter_context(unittest.mock.patch(
                'dvc.prompt.input', new=_input))

        log.debug("Calling DVC main with args: %s", args)

        logs = contexts.enter_context(catch_logs('dvc'))

        with contexts:
            retcode = self.module.main.main(args)

        logs = logs.getvalue()
        if retcode != 0:
            raise self.DvcError(logs)
        if not hide_output:
            print(logs)
        return logs

    def is_cached(self, obj_hash):
        path = self.make_cache_path(obj_hash)
        if not osp.isfile(path):
            return False

        if obj_hash.endswith('.dir'):
            objects = json.load(path)
            for entry in objects:
                if not osp.isfile(self.make_cache_path(entry['md5'])):
                    return False

        return True

    def make_cache_path(self, obj_hash, root=None):
        assert len(obj_hash) == 40
        if not root:
            root = osp.join(self._project_dir, '.dvc', 'cache')
        return osp.join(root, obj_hash[:2], obj_hash[2:])

    IgnoreMode = IgnoreMode

    def ignore(self, paths: List[str], mode: Optional[IgnoreMode] = None,
            dvcignore: Optional[str] = None):
        if not dvcignore:
            dvcignore = '.gitignore'
        repo_root = self._project_dir
        dvcignore = osp.abspath(osp.join(repo_root, dvcignore))
        assert dvcignore.startswith(repo_root), dvcignore

        _update_ignore_file(paths, repo_root=repo_root,
            mode=mode, filepath=dvcignore)

class Tree:
    # can be:
    # - detached
    # - attached to the work dir
    # - attached to the index dir
    # - attached to a revision

    @classmethod
    def _read_config_v1(cls, config):
        config = Config(config)
        config.remove('subsets')
        config.remove('format_version')

        config = cls._read_config_v2(config)
        if osp.isdir(osp.join(config.project_dir, config.dataset_dir)):
            name = generate_next_name(list(config.sources), 'source',
                sep='-', default='1')
            config.sources[name] = {
                'url': config.dataset_dir,
                'format': DEFAULT_FORMAT,
            }
        return config

    @classmethod
    def _read_config_v2(cls, config):
        return TreeConfig(config)

    @classmethod
    def _read_config(cls, config):
        if config:
            version = config.get('format_version')
        else:
            version = None
        if version == 1:
            return cls._read_config_v1(config)
        elif version in {None, 2}:
            return cls._read_config_v2(config)
        else:
            raise ValueError("Unknown project config file format version '%s'. "
                "The only known are: 1, 2" % version)

    def __init__(self, config: Optional[TreeConfig] = None,
            env: Optional[Environment] = None,
            parent: 'Project' = None, rev: Optional[str] = None):
        self._config = self._read_config(config)
        if env is None:
            env = Environment(self._config)
        elif config is not None:
            raise ValueError("env can only be provided when no config provided")
        self._env = env or Environment(self)
        self._parent = parent
        self._rev = rev

        self._sources = ProjectSources(self)
        self._models = ProjectModels(self)
        self._remotes = ProjectRemotes(self)
        self._targets = ProjectBuildTargets(self)

    @error_rollback('on_error', implicit=True)
    def dump(self, save_dir: Union[None, str] = None):
        config = self.config

        config.project_dir = save_dir or config.project_dir
        assert config.project_dir
        project_dir = config.project_dir
        save_dir = osp.join(project_dir, config.env_dir)

        if not osp.exists(project_dir):
            on_error.do(shutil.rmtree, project_dir, ignore_errors=True)
        if not osp.exists(save_dir):
            on_error.do(shutil.rmtree, save_dir, ignore_errors=True)
        os.makedirs(save_dir, exist_ok=True)

        config.dump(osp.join(save_dir, config.project_filename))

    @property
    def sources(self) -> ProjectSources:
        return self._sources

    @property
    def models(self) -> ProjectModels:
        return self._models

    @property
    def build_targets(self) -> ProjectBuildTargets:
        return self._targets

    @property
    def config(self) -> Config:
        return self._config

    @property
    def env(self) -> Environment:
        return self._env

    @property
    def rev(self) -> str:
        return self._rev

    @property
    def detached(self) -> bool:
        return self._parent is None

    def make_dataset(self, target: Optional[str] = None) -> IDataset:
        if target is None:
            target = 'project'
        return self.build_targets.make_dataset(target)

class Project:
    @staticmethod
    def find_project_dir(path):
        if path.endswith(ProjectLayout.aux_dir) and osp.isdir(path):
            return path

        temp_path = osp.join(path, ProjectLayout.aux_dir)
        if osp.isdir(temp_path):
            return temp_path

        return None

    def __init__(self, path: Optional[str] = None):
        if not path:
            path = osp.curdir
        found_path = self.find_project_dir(path)
        if not found_path:
            raise ProjectNotFoundError("Can't find project at '%s'" % path)

        self._aux_dir = found_path
        self._root_dir = osp.dirname(found_path)

        # DVC requires Git to be initialized
        GitWrapper.import_module()
        DvcWrapper.import_module()
        self._git = GitWrapper(self._root_dir)
        self._dvc = DvcWrapper(self._root_dir)

    @classmethod
    def init(cls, path):
        existing_project = cls.find_project_dir(path)
        if existing_project:
            raise ProjectAlreadyExists("Can't create project in '%s': " \
                "a project already exists" % path)

        if not path.endswith(ProjectLayout.aux_dir):
            path = osp.join(path, ProjectLayout.aux_dir)
        os.makedirs(path, exists_ok=True)

        os.makedirs(osp.join(path, ProjectLayout.cache_dir))
        os.makedirs(osp.join(path, ProjectLayout.dvc_temp_dir))

        project = Project(path)
        project._git.init()
        project._dvc.init()

        # TODO: find which paths need to be ignored
        # project._git.ensure_ignored()
        # project._dvc.ensure_ignored()

        return project

    @property
    def working_tree(self) -> Tree:
        return self.get_rev(None)

    @property
    def index(self) -> Tree:
        return self.get_rev('index')

    @property
    def head(self) -> Tree:
        return self.get_rev('HEAD')

    def get_rev(self, rev: str) -> Tree:
        """
        Ref convetions:
        - None or "" - working dir
        - "index" - index
        - "<40 symbols>" - revision hash
        """

        obj_type, obj_hash = self._parse_ref(rev)
        assert obj_type == 'tree', obj_type

        if not obj_hash:
            tree_config = TreeConfig.parse(
                osp.join(self._aux_dir, TreeLayout.conf_file))
            # TODO: adjust paths in config
            tree = Tree(tree_config, parent=self, rev=obj_hash)
        elif obj_hash is 'index':
            tree_config = TreeConfig.parse(osp.join(self._aux_dir,
                ProjectLayout.index_tree_dir, TreeLayout.conf_file))
            # TODO: adjust paths in config
            tree = Tree(tree_config, parent=self, rev=obj_hash)
        elif not self.is_rev_cached(obj_hash):
            self._materialize_rev(obj_hash)

            rev_dir = self._make_cache_path(obj_hash)
            tree_config = TreeConfig.parse(osp.join(rev_dir,
                TreeLayout.conf_file))
            # TODO: adjust paths in config
            tree = Tree(tree_config, parent=self, rev=obj_hash)
        return tree

    def is_rev_cached(self, rev: str) -> bool:
        obj_hash = self._parse_ref(rev)
        return self._is_cached(obj_hash)

    def is_obj_cached(self, obj_hash: str) -> bool:
        return self._is_cached(obj_hash) or \
            self._can_retrieve_from_vcs_cache(obj_hash)

    def _parse_ref(self, ref: str) -> Tuple[str, str]:
        try:
            obj = self.git.rev_parse(ref)
            assert obj.type == 'commit', obj

            obj_type = 'tree'
        except Exception as e:
            if isinstance(e, AssertionError):
                raise

        try:
            assert self._dvc.is_cached(ref), ref
            obj_hash = ref
            obj_type = 'blob'
        except Exception:
            raise UnknownRefError("Can't parse ref '%s'" % ref)

        return obj_type, obj_hash

    def _materialize_rev(self, rev):
        tree = self._git.get_tree(rev)
        obj_dir = self._make_cache_path(tree.hexsha)
        self._git.write_tree(tree, obj_dir)

    def _is_cached(self, obj_hash):
        return osp.isdir(self._make_cache_path(obj_hash))

    def _make_cache_path(self, obj_hash, root=None):
        assert len(obj_hash) == 40
        if not root:
            root = osp.join(self._aux_dir, ProjectLayout.aux_dir,
                ProjectLayout.cache_dir)
        return osp.join(root, obj_hash[:2], obj_hash[2:])

    def _can_retrieve_from_vcs_cache(self, rev_hash):
        return self._dvc.is_cached(rev_hash)

    def download_source(self, source):
        raise NotImplementedError()
        # dvc = self.dvc

        # temp_dir = make_temp_dir(
        #     osp.join(self.config.project_dir, ProjectLayout.AUX_DIR, 'temp'))
        # dvc_config = load_dvc_config(self, source)
        # dvc_config_path = osp.join(temp_dir, 'config.dvc')
        # write_dvc_config(dvc_config, )
        # dvc.download_source(dvc_config, temp_dir)

        # source.hash = dvc.compute_source_hash(temp_dir)
        # obj_dir = _make_obj_path(self, source.hash)
        # shutil.move(temp_dir, obj_dir) # moves _into_ obj_dir

    # cache
    # repo (remote)

    def add(self, sources: List[str]):
        """
        Copies changes from working copy to index.
        """

        if not sources:
            raise ValueError("Expected at least one source path to add")

        index_cache_dir = osp.join(self._aux_dir,
            ProjectLayout.index_cache_dir)

        for s in sources:
            if not s in self.working_tree.sources:
                raise UnknownSourceError(s)

            source_dir = osp.join(self._root_dir, s)

            dir_hash, dir_objs = self._dvc.compute_hash(source_dir)
            index_obj_dir = self._make_cache_path(dir_hash,
                root=index_cache_dir)
            if self._is_cached(dir_hash):
                os.link(index_obj_dir, self._make_cache_path(dir_hash))
            else:
                with open(osp.join(index_obj_dir, 'meta'), 'w') as f:
                    json.dump(dir_objs, f, sort_keys=True)
                shutil.copy(source_dir, osp.join(index_obj_dir, 'data'))

            source_config = self.working_tree.config.sources[s]
            source_config.hash = dir_hash
            self.index.config.sources[s] = source_config

        self.index.dump(osp.join(self._aux_dir,
            ProjectLayout.index_tree_dir, TreeLayout.conf_file))

    def commit(self, message: str):
        """
        Copies tree and objects from index to cache.
        Creates a new commit. Moves the HEAD pointer to the new commit.
        """

        index_cache_dir = osp.join(self._aux_dir,
            ProjectLayout.index_cache_dir)

        for s_name, s_conf in self.index.config.sources.items():
            index_obj_path = self._make_cache_path(s_conf.hash,
                root=index_cache_dir)

            if not osp.exists(index_obj_path):
                raise NotADirectoryError(index_obj_path)

            if osp.islink(index_obj_path):
                if not osp.lexists(index_obj_path):
                    raise NotADirectoryError(index_obj_path)
                continue

            cache_obj_path = self._make_cache_path(s_conf.hash)
            shutil.move(index_obj_path, cache_obj_path)

        index_tree_dir = osp.join(self._aux_dir, ProjectLayout.index_tree_dir)
        self._git.add(index_tree_dir, base=index_tree_dir)
        head = self._git.commit(message)
        shutil.move(index_tree_dir, self._make_cache_path(head))

        rmtree(osp.join(self._aux_dir, ProjectLayout.index_dir),
            ignore_errors=True)

    def checkout(self, rev: Optional[str] = None,
            targets: Union[None, str, List[str]] = None):
        """
        Copies tree and objects from cache to working tree.

        Sets HEAD to the specified revision, unless targets specified.
        When targets specified, only copies objects from cache to working tree.
        """

        assert targets is None or isinstance(targets, (str, list)), targets
        if targets is None:
            targets = []
        elif isinstance(targets, str):
            targets = [targets]
        targets = targets or []
        for i, t in enumerate(targets):
            if not osp.exists(t):
                targets[i] = self.dvc_filepath(t)

        # order matters
        self.git.checkout(rev, targets) # TODO: need to reload the project
        self.dvc.checkout(targets)

    def is_ref(self, ref: str) -> bool:
        return self._git.is_ref(ref)

    def has_commits(self) -> bool:
        return self._git.has_commits()

    @classmethod
    def from_dataset(cls, path: str, dataset_format: Optional[str] = None,
            env: Optional[Environment] = None, **format_options) -> 'Project':
        """
        A convenience function to create a project from a given dataset.
        """
        raise NotImplementedError()

        if env is None:
            env = Environment()

        if not dataset_format:
            matches = env.detect_dataset(path)
            if not matches:
                raise DatumaroError(
                    "Failed to detect dataset format automatically")
            if 1 < len(matches):
                raise DatumaroError(
                    "Failed to detect dataset format automatically:"
                    " data matches more than one format: %s" % \
                    ', '.join(matches))
            dataset_format = matches[0]
        elif not env.is_format_known(dataset_format):
            raise KeyError("Unknown format '%s'. To make it "
                "available, add the corresponding Extractor implementation "
                "to the environment" % dataset_format)

        project = Project(env=env)
        project.sources.add('source', {
            'url': path,
            'format': dataset_format,
            'options': format_options,
        })
        return project

    # def push(self, targets: Optional[List[str]] = None,
    #         remote: Optional[str] = None, repository: Optional[str] = None):
    #     """
    #     Pushes the local DVC cache to the remote storage.
    #     Pushes local Git changes to the remote repository.

    #     If not provided, uses the default remote storage and repository.
    #     """

    #     if self.detached:
    #         log.debug("The project is in detached mode, skipping push.")
    #         return

    #     if not self.writeable:
    #         raise ReadonlyProjectError("Can't push in a read-only repository")

    #     assert targets is None or isinstance(targets, (str, list)), targets
    #     if targets is None:
    #         targets = []
    #     elif isinstance(targets, str):
    #         targets = [targets]
    #     targets = targets or []
    #     for i, t in enumerate(targets):
    #         if not osp.exists(t):
    #             targets[i] = self.dvc_filepath(t)

    #     # order matters
    #     self.dvc.push(targets, remote=remote)
    #     self.git.push(remote=repository)

    # def pull(self, targets: Union[None, str, List[str]] = None,
    #         remote: Optional[str] = None, repository: Optional[str] = None):
    #     """
    #     Pulls the local DVC cache data from the remote storage.
    #     Pulls local Git changes to the remote repository.

    #     If not provided, uses the default remote storage and repository.
    #     """

    #     if self.detached:
    #         log.debug("The project is in detached mode, skipping pull.")
    #         return

    #     if not self.writeable:
    #         raise ReadonlyProjectError("Can't pull in a read-only repository")

    #     assert targets is None or isinstance(targets, (str, list)), targets
    #     if targets is None:
    #         targets = []
    #     elif isinstance(targets, str):
    #         targets = [targets]
    #     targets = targets or []
    #     for i, t in enumerate(targets):
    #         if not osp.exists(t):
    #             targets[i] = self.dvc_filepath(t)

    #     # order matters
    #     self.git.pull(remote=repository)
    #     self.dvc.pull(targets, remote=remote)

    # def check_updates(self,
    #         targets: Union[None, str, List[str]] = None) -> List[str]:
    #     if self.detached:
    #         log.debug("The project is in detached mode, "
    #             "skipping checking for updates.")
    #         return

    #     if not self.writeable:
    #         raise ReadonlyProjectError(
    #             "Can't check for updates in a read-only repository")

    #     assert targets is None or isinstance(targets, (str, list)), targets
    #     if targets is None:
    #         targets = []
    #     elif isinstance(targets, str):
    #         targets = [targets]
    #     targets = targets or []
    #     for i, t in enumerate(targets):
    #         if not osp.exists(t):
    #             targets[i] = self.dvc_filepath(t)

    #     updated_refs = self.git.check_updates()
    #     updated_remotes = self.remotes.check_updates(targets)
    #     return updated_refs, updated_remotes

    # def fetch(self, targets: Union[None, str, List[str]] = None):
    #     if self.detached:
    #         log.debug("The project is in detached mode, skipping fetch.")
    #         return

    #     if not self.writeable:
    #         raise ReadonlyProjectError("Can't fetch in a read-only repository")

    #     assert targets is None or isinstance(targets, (str, list)), targets
    #     if targets is None:
    #         targets = []
    #     elif isinstance(targets, str):
    #         targets = [targets]
    #     targets = targets or []
    #     for i, t in enumerate(targets):
    #         if not osp.exists(t):
    #             targets[i] = self.dvc_filepath(t)

    #     self.git.fetch()
    #     self.dvc.fetch(targets)

    # def tag(self, name: str):
    #     self.git.tag(name)




def merge_projects(a, b, strategy: MergeStrategy = None):
    raise NotImplementedError()

def compare_projects(a, b, **options):
    raise NotImplementedError()


def load_project_as_dataset(url):
    return Project(url).work_dir.make_dataset()

def parse_target_revpath(revpath: str):
    sep_pos = revpath.find(':')
    if -1 < sep_pos:
        rev = revpath[:sep_pos]
        target = revpath[sep_pos:]
    else:
        rev = ''
        target = revpath

    return rev, target

IgnoreMode = Enum('IgnoreMode', ['rewrite', 'append', 'remove'])

def _update_ignore_file(paths: List[str], repo_root: str, filepath: str,
        mode: Optional[IgnoreMode] = None):
    def _make_ignored_path(path):
        path = osp.join(repo_root, osp.normpath(path))
        assert path.startswith(repo_root), path
        return osp.relpath(path, repo_root)

    mode = parse_str_enum_value(mode, IgnoreMode, IgnoreMode.append)
    paths = [_make_ignored_path(p) for p in paths]

    openmode = 'r+'
    if not osp.isfile(filepath):
        openmode = 'w+' # r+ cannot create, w+ truncates
    with open(filepath, openmode) as f:
        if mode in {IgnoreMode.append, IgnoreMode.remove}:
            paths_to_write = set(
                line.split('#', maxsplit=1)[0] \
                    .split('/', maxsplit=1)[-1].strip()
                for line in f
            )
            f.seek(0)
        else:
            paths_to_write = set()

        if mode in {IgnoreMode.append, IgnoreMode.rewrite}:
            paths_to_write.update(paths)
        elif mode == IgnoreMode.remove:
            for p in paths:
                paths_to_write.discard(p)

        paths_to_write = sorted(p for p in paths_to_write if p)
        f.write('# The file is autogenerated by Datumaro\n')
        f.writelines('\n'.join(paths_to_write))
        f.truncate()
