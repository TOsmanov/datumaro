Welcome to Datumaro documentation!
##################################

Welcome to the documentation for the Dataset Management Framework (Datumaro).

The Datumaro is a free framework and CLI tool for building, transforming,
and analyzing datasets.
It is developed and used by Intel to build, transform, and analyze annotations
and datasets in a large number of :doc:`/docs/user-manual/supported_formats/`.

Our documentation provides information for AI researchers, developers,
and teams, who are working with datasets and annotations.

.. mermaid::

   %%{init { 'theme':'base' }}%%
   flowchart LR
      datasets[(VOC dataset<br/>+<br/>COCO datset<br/>+<br/>CVAT annotation)]
      datumaro{Datumaro}
      dataset[dataset]
      annotation[Annotation tool]
      training[Model training]
      publication[Publication, statistics etc]
      datasets-->datumaro
      datumaro-->dataset
      dataset-->annotation & training & publication

Documentation
-------------

.. toctree::
   :maxdepth: 1
   :glob:

   /docs/getting_started.md
   /docs/design.md
   /docs/user-manual/user-manual.rst
   /docs/developer_manual.md
   /docs/formats/formats.rst
   /docs/plugins/plugins.rst
   /docs/contributing.md
   /docs/release_notes.md

API Documentation
-----------------

The Datumaro API provides access to functions for building composite datasets
and re-iterate through them, create and maintain datasets,
store datasets, and so on.
This Documentation should point you toward the right
Datumaro integration process.

.. toctree::
   :maxdepth: 1
   :glob:

   /api/cli/*
   /api/components/*
   /api/plugins/*
   /api/util/util/util.rst

Indices and tables
******************

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
