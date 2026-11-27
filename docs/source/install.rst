Installation
============

**terraform-worker** is `published on pypi <https://pypi.org/project/terraform-worker/>`_.
To install **terraform-worker** using `pip <https://pypi.org/project/pip/>`_, run the
following:

.. code-block:: bash

    % pip install --user terraform-worker

If using `uv <https://docs.astral.sh/uv/>`_:

.. code-block:: bash

    % uv tool install terraform-worker

or, to add it to an existing uv project:

.. code-block:: bash

    % uv add terraform-worker

Dependencies
------------

Since **terraform-worker** is a wrapper, it depends on terraform version 0.12+.
