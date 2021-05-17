.. zulu documentation master file, created by
   sphinx-quickstart on Mon May  3 11:27:55 2021.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

Welcome to terraform-worker's documentation!
============================================

The **terraform-worker** is a terraform wrapper which emphasizes configuration simplicity for 
complex orchestrations.  The **terraform-worker** works by reading a configuration of terraform
provider, variable and module :ref:`definitions`, gathering provider plugins and remote terraform
sources, and then serially executing the terraform operations in a local temporary directory. The
**terraform-worker** supports passing orchestration values through a pipeline of terraform operations
via the `data\.terraform_remote_state <https://www.terraform.io/docs/language/state/remote-state-data.html>`_
data source.

.. toctree::
   :maxdepth: 1
   :caption: Getting Started:

   features
   install
   quick-start
   commands

.. toctree::
   :maxdepth: 1
   :caption: Configurations Details:

   concepts
   hooks
   schema

Indices and tables
==================

* :ref:`genindex`
* :ref:`search`
