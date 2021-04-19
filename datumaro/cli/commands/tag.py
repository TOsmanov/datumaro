# Copyright (C) 2020 Intel Corporation
#
# SPDX-License-Identifier: MIT

import argparse

from ..util.project import load_project


def build_parser(parser_ctor=argparse.ArgumentParser):
    parser = parser_ctor(help="Give a name (tag) to the current revision")

    parser.add_argument('name',
        help="Name (tag) for the current revision")
    parser.add_argument('-p', '--project', dest='project_dir', default='.',
        help="Directory of the project to operate on (default: current dir)")
    parser.set_defaults(command=tag_command)

    return parser

def tag_command(args):
    project = load_project(args.project_dir)

    project.vcs.tag(args.name)

    return 0
