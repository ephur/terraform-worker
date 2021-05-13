JSON Schema
===========

The configuration is the crux of **terraform-worker**.  Similar to a ``docker-compose`` file, the
configuration provides the details on how an orchestration will unfold.

A **terraform-worker** configuration can be written in YAML, JSON or HCL.

.. index::
   single: configuration schema

Here is the JSON schema:

.. code-block:: json

   {
       "definitions": {},
       "$schema": "http://json-schema.org/draft-07/schema#",
       "$id": "https://example.com/object1620139083.json",
       "title": "Root",
       "type": "object",
       "required": [
           "terraform"
       ],
       "properties": {
           "terraform": {
               "$id": "#root/terraform",
               "title": "Terraform",
               "type": "object",
               "required": [
                   "providers",
                   "definitions"
               ],
               "properties": {
                   "providers": {
                       "$id": "#root/terraform/providers",
                       "title": "Providers",
                       "type": "object",
                       "patternProperties": {
                           "^.*$": { "type": "object" }
                       },
                       "additionalProperties": false
                   },
                   "terraform_vars": {
                       "$id": "#root/terraform/terraform_vars",
                       "title": "Terraform_vars",
                       "type": "object",
                       "patternProperties": {
                           "^.*$": { "type": "object" }
                       },
                       "additionalProperties": false
                   },
                   "definitions": {
                       "$id": "#root/terraform/definitions",
                       "title": "Definitions",
                       "type": "object",
                       "patternProperties": {
                           "^.*$": { "type": "object" },
                           "properties": {
                               "path": {
                                   "$id": "#root/terraform/definitions/<definition-name>/path",
                                   "title": "Path",
                                   "type": "string",
                                   "default": "",
                                   "examples": [
                                       "/definitions/aws/network-existing"
                                   ],
                                   "pattern": "^.*$"
                               },
                               "terraform_vars": {
                                   "$id": "#root/terraform/definitions/<definition-name>/terraform_vars",
                                   "title": "Terraform vars",
                                   "type": "object",
                                   "patternProperties": {
                                       "^.*$": { "type": "object" }
                                   },
                                   "additionalProperties": false
                               },
                               "remote_vars": {
                                   "$id": "#root/terraform/definitions/<definition-name>/remote_vars",
                                   "title": "Remote vars",
                                   "type": "object",
                                   "patternProperties": {
                                       "^.*$": { "type": "string" }
                                   },
                                   "additionalProperties": false
                               }
                           }
                       }
                   }
               }
           }
       }
   }
