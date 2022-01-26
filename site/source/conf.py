# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.

import os
import sys
import shutil
import glob
import re

sys.path.insert(0, os.path.abspath('../..'))

import git
from datumaro.version import VERSION

# -- Project information -----------------------------------------------------

project = 'Datumaro Documentation'
author = 'Intel'

# The full version, including alpha/beta/rc tags
release = VERSION


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.napoleon', # Support for NumPy and Google style docstrings
    'sphinx.ext.autodoc',  # Core library for html generation from docstrings
    'sphinx.ext.viewcode', # Find the source files
    'sphinxcontrib.mermaid', # Extension allows you to embed Mermaid graphs
    'sphinx_copybutton', # Copy buttons for code blocks
]

source_parsers  =  {
   '.md' :  'recommonmark.parser.CommonMarkParser' ,
}

source_suffix  =  [ '.rst' ,  '.md' ]

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates', ]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = [ ]


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'
html_theme_path = ['_themes', ]
html_theme_options = {
    'prev_next_buttons_location': 'bottom',
    'style_external_links': True,
    'vcs_pageview_mode': '',
    'style_nav_header_background': '#30638E',
    # Toc options
    'titles_only': False,
    'display_version': True,
}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static', ]
html_css_files = ['custom.css', ]

# -- Extension configuration -------------------------------------------------
# sphinx.ext.autodoc
autodoc_docstring_signature = True
autodoc_member_order = 'bysource'
# sphinxcontrib.mermaid
mermaid_params = [
    '--width', '500',
]
# sphinxcontrib-versioning
# scv_whitelist_branches = ('v0.1.11', 'v0.2.1', 'v0.2.2')
scv_whitelist_tags = (re.compile(r'^v\d+\.\d+\.\d+$'),)

# Prepair source files for past versions.

# def prepair_files(cwd):
#     index_md = os.path.join(cwd, 'site', 'source', 'docs', '_index.md')
#     if os.path.exists(index_md):
#         os.remove(index_md)
#     git.checkout('develop', '--', 'site/source/index.rst')
#     git.checkout('develop', '--', 'site/source/formats/formats.rst')
#     git.checkout('develop', '--', 'site/source/plugins/plugins.rst')
#     git.checkout('develop', '--', 'site/source/user-manual/user-manual.rst')
#     git.checkout('develop', '--', 'site/source/user-manual/command-reference/command-reference.rst')

# def prepair_headers(destination):
#     files = glob.iglob(os.path.join(destination, '**', '*.md'), recursive=True)
#     for file in files:
#         with open(file, 'r+') as f:
#             lines = f.readlines()
#         for i, line in enumerate(lines):
#             if '```mermaid' in line:
#                 lines[i] = line.replace("```mermaid","```{mermaid}")
#             if "---" in line:
#                 lines[i] = ''
#             if "title: '" in line:
#                 title = '# ' + line[8:-2] + '\n'
#                 lines[i] = title
#             if "description: '" in line:
#                 description = line[14:-2] + '\n'
#                 lines[i] = description
#             exclude_list = [
#                 "linkTitle: '",
#                 "weight:",
#                 ]
#             for a in exclude_list:
#                 if a in line:
#                     lines[i] = ''
#         with open(file, 'w') as f:
#             f.writelines(lines)

# def prepair_current_repo():
#     cwd = os.getcwd()
#     destination = os.path.join(cwd, 'site', 'source')
#     prepair_files(cwd, destination)
#     prepair_headers(destination)

# def source_read_handler(app, docname, source):
#     if os.path.exists(os.path.join('site', 'source', 'content')):
#         prepair_current_repo()

# Prepair docstring for autodoc

include_members_list = [
    '__init__',
    '__iter__',
    '__eq__',
    '__len__',
    '__contains__',
    '__getitem__',
]

def skip_member(app, what, name, obj, skip, options):
    if all(name != a for a in include_members_list):
        return name.startswith('_')

def replace(app, what, name, obj, options, lines):
    for i, line in enumerate(lines):
        if line:
            if not "'|n'" in lines[i]:
                if not "'|s'" in lines[i]:
                    lines[i] = lines[i].replace("|n", "\n").replace("|s", " ")

def setup(app):
    app.connect('autodoc-skip-member', skip_member)
    app.connect('autodoc-process-docstring', replace)
    # app.connect('source-read', source_read_handler)
