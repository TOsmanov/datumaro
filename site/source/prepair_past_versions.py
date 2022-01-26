import os
import shutil
import glob
import subprocess

import git

old_versions = ['v0.1.11', 'v0.2.1', 'v0.2.2']

cwd = 'C:\\Git\\migrating-docs-to-sphinx\\datumaro' # for test
docs_dir = os.path.join(cwd, 'site', 'content', 'en', 'docs')
images_dir = os.path.join(cwd, 'site', 'content', 'en', 'images')

def prepair(old_versions):
    for ver in old_versions:
        destination = os.path.join(cwd, 'site', 'source', ver)
        prepair_repo(repo, docs_dir, ver, destination)
        prepair_files(ver, destination)
        prepair_headers(destination)
        generate_docs(destination, cwd, ver)

def prepair_repo(repo, docs_dir, ver, destination):
    repo.git.checkout(ver, '--', docs_dir)
    if ver != 'v0.1.11':
        repo.git.checkout(ver, '--', images_dir)
    if os.path.exists(os.path.join(destination, 'docs')):
        shutil.rmtree(os.path.join(destination, 'docs'))
    shutil.move(docs_dir, os.path.join(destination, 'docs')) # ToDo remake move with use .write
    if os.path.exists(os.path.join(images_dir, 'images')):
        shutil.move(images_dir, os.path.join(destination, 'images')) # ToDo remake move with use .write

def prepair_files(ver, destination):
    index_md = os.path.join(cwd, 'site', 'source', ver, 'docs', '_index.md')
    if os.path.exists(index_md):
        os.remove(index_md)
    last_ver = os.path.join(cwd, 'site', 'source', 'docs')
    source_dir = os.path.join(cwd, 'site', 'source')
    destination_docs = os.path.join(destination, 'docs')
    shutil.copy(os.path.join(source_dir, 'index.rst'), destination)
    shutil.copy(os.path.join(last_ver, 'formats', 'formats.rst'), os.path.join(destination_docs, 'formats'))
    shutil.copy(os.path.join(last_ver, 'plugins', 'plugins.rst'), os.path.join(destination_docs, 'plugins'))
    shutil.copy(os.path.join(last_ver, 'user-manual', 'user-manual.rst'), os.path.join(destination_docs, 'user-manual'))

def prepair_headers(destination):
    files = glob.iglob(os.path.join(destination, '**', '*.md'), recursive=True)
    for file in files:
        with open(file, 'r+') as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            if '```mermaid' in line:
                lines[i] = line.replace("```mermaid","```{mermaid}")
            if "---" in line:
                lines[i] = ''
            if "title: '" in line:
                title = '# ' + line[8:-2] + '\n'
                lines[i] = title
            if "description: '" in line:
                description = line[14:-2] + '\n'
                lines[i] = description
            exclude_list = [
                "linkTitle: '",
                "weight:",
                ]
            for a in exclude_list:
                if a in line:
                    lines[i] = ''

        with open(file, 'w') as f:
            f.writelines(lines)
def generate_docs(destination, cwd, ver):
    # sphinx-build -a -n -c ..\conf site\source site\build
    conf_dir = os.path.join(cwd, 'site', 'source')
    build_dir = os.path.join(cwd, 'site', 'source', 'build', ver)
    subprocess.run([
        'sphinx-build',
        '-a',
        '-n',
        '-c',
        conf_dir,
        destination,
        build_dir,
        ])

if __name__ == "__main__":
    repo_root = 'C:\\Git\\migrating-docs-to-sphinx\\datumaro' # for test
    # repo_root = os.getcwd()
    repo = git.Repo(repo_root)

    prepair(old_versions)
