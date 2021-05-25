terraform {
  providers {
    aws = {
      vars = {
        region = "{{ aws_region }}"
        version = "2.63.0"
      }
    }
    null = {
      vars = {
        version = "~> 2.1.2"
      }
    }
    random = {
      vars = {
        version = "~> 2.3.0"
      }
    }
  }

  terraform_vars {
    environment = "dev"

    makerenvironment = "Development"
    makerenvironment = "dev"
    region = "{{ aws_region }}"
  }
  definitions {
    tags = {
      path = "/definitions/aws/misc-tags"

      terraform_vars = {
        mf_version = 1.0
      }
    }

    base_network = {
      path = "/definitions/aws/network-new"

      remote_vars = {
        tag_map = "tags.outputs.tag_map"
      }
    }

    dev_artifact_repo = {
      path = "/definitions/aws/misc-s3-storage"

      remote_vars = {
        tag_map = "tags.outputs.tag_map"
      }
    }

    stage_artifact_repo = {
      path = "/definitions/aws/misc-s3-storage"

      remote_vars = {
        tag_map = "tags.outputs.tag_map"
      }

      terraform_vars = {
        environment = "stage"
        extravironment = "realstage"
      }
    }

    sagemaker_notebook = {
      path = "/definitions/aws/sagemaker-notebook"

      remote_vars = {
        code_repo_url = "customer_churn_model_repository.outputs.model_repo_url"
        kms_arn = "dev_artifact_repo.outputs.kms_arn"
        model_repo_id = "customer_churn_model_repository.outputs.model_repo_id"
        public_subnets = "base_network.outputs.public_subnet_ids"
        sagemaker_role = "dev_sagemaker_roles.outputs.sagemaker_role_arn"
        sagemaker_sg = "dev_sagemaker_roles.outputs.sagemaker_sg"
      }

      terraform_vars = {
        num = 1
      }
    }
  }
}
