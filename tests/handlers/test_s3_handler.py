import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import boto3
import botocore
import pytest
from moto import mock_aws

from tfworker.exceptions import HandlerError
from tfworker.handlers.s3 import S3Handler


@pytest.fixture
def handler_with_bucket(mock_app_state):
    """Return S3Handler with created bucket within moto"""
    with mock_aws():
        s3 = boto3.client("s3", region_name=mock_app_state.root_options.backend_region)
        s3.create_bucket(Bucket=mock_app_state.root_options.backend_bucket)
        handler = S3Handler()
        yield handler, s3


class TestGetRemoteFile:
    def test_no_run_id(self, mock_app_state):
        mock_app_state.root_options.run_id = None
        handler = S3Handler()
        assert handler.get_remote_file("def") == "prefix/def/terraform.tfplan"

    def test_with_run_id(self, mock_app_state):
        mock_app_state.root_options.run_id = "1234"
        handler = S3Handler()
        assert handler.get_remote_file("def") == "prefix/1234/def/terraform.tfplan"


class TestS3GetPlan:
    def test_missing_remote(self, handler_with_bucket, tmp_path):
        handler, _ = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        assert handler._s3_get_plan(planfile, "missing") is False
        assert not planfile.exists()

    def test_downloads_plan(self, handler_with_bucket, tmp_path):
        handler, s3 = handler_with_bucket
        remotefile = handler.get_remote_file("def")
        s3.put_object(Bucket=handler.bucket, Key=remotefile, Body=b"data")
        planfile = tmp_path / "plan.tfplan"
        assert handler._s3_get_plan(planfile, remotefile) is True
        assert planfile.exists()

    def test_error_raises(self, handler_with_bucket, tmp_path, monkeypatch):
        handler, _ = handler_with_bucket

        def raise_error(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "500"}},
                "DownloadFile",
            )

        monkeypatch.setattr(handler.s3_client, "download_file", raise_error)
        with pytest.raises(HandlerError):
            handler._s3_get_plan(tmp_path / "plan.tfplan", "somefile")


class TestS3PutPlan:
    def test_does_not_upload_empty(self, handler_with_bucket, tmp_path):
        handler, s3 = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        planfile.write_text("")
        remotefile = handler.get_remote_file("def")
        assert handler._s3_put_plan(planfile, remotefile) is False
        objects = s3.list_objects(Bucket=handler.bucket).get("Contents")
        assert objects is None

    def test_uploads_file(self, handler_with_bucket, tmp_path):
        handler, s3 = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        planfile.write_text("data")
        remotefile = handler.get_remote_file("def")
        assert handler._s3_put_plan(planfile, remotefile) is True
        body = s3.get_object(Bucket=handler.bucket, Key=remotefile)["Body"].read()
        assert body == b"data"

    def test_error_raises(self, handler_with_bucket, tmp_path, monkeypatch):
        handler, _ = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        planfile.write_text("data")

        def raise_error(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "500"}},
                "UploadFile",
            )

        monkeypatch.setattr(handler.s3_client, "upload_file", raise_error)
        with pytest.raises(HandlerError):
            handler._s3_put_plan(planfile, handler.get_remote_file("def"))


class TestS3DeletePlan:
    def test_delete_existing(self, handler_with_bucket):
        handler, s3 = handler_with_bucket
        remotefile = handler.get_remote_file("def")
        s3.put_object(Bucket=handler.bucket, Key=remotefile, Body=b"data")
        assert handler._s3_delete_plan(remotefile) is True
        assert s3.list_objects(Bucket=handler.bucket).get("Contents") is None

    def test_error_raises(self, handler_with_bucket, monkeypatch):
        handler, _ = handler_with_bucket

        def raise_error(*args, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "500"}},
                "DeleteObject",
            )

        monkeypatch.setattr(handler.s3_client, "delete_object", raise_error)
        with pytest.raises(HandlerError):
            handler._s3_delete_plan("bad")


class TestVerifyLineage:
    def create_plan(self, tmp_path, serial=1, lineage="abcd"):
        plan = tmp_path / "plan.tfplan"
        with ZipFile(plan, "w") as z:
            z.writestr("tfstate", json.dumps({"serial": serial, "lineage": lineage}))
        return plan

    def upload_state(self, s3, bucket, key, serial=1, lineage="abcd"):
        body = json.dumps({"serial": serial, "lineage": lineage}).encode()
        s3.put_object(Bucket=bucket, Key=key, Body=body)

    def test_matches(self, handler_with_bucket, tmp_path):
        handler, s3 = handler_with_bucket
        plan = self.create_plan(tmp_path)
        state_key = f"{handler.prefix}/def/terraform.tfstate"
        self.upload_state(s3, handler.bucket, state_key)
        assert handler._verify_lineage(plan, state_key) is True

    def test_mismatch(self, handler_with_bucket, tmp_path):
        handler, s3 = handler_with_bucket
        plan = self.create_plan(tmp_path, serial=1, lineage="x")
        state_key = f"{handler.prefix}/def/terraform.tfstate"
        self.upload_state(s3, handler.bucket, state_key, serial=2, lineage="y")
        assert handler._verify_lineage(plan, state_key) is False

    def test_state_missing_raises(self, handler_with_bucket, tmp_path):
        handler, _ = handler_with_bucket
        plan = self.create_plan(tmp_path)
        with pytest.raises(HandlerError):
            handler._verify_lineage(plan, "missing")


class TestPreApply:
    def test_calls_delete_for_plan_and_log(self, handler_with_bucket):
        handler, _ = handler_with_bucket
        definition = MagicMock()
        definition.name = "def"
        with patch.object(handler, "_s3_delete_plan") as delete_mock:
            handler._pre_apply(definition)
            remotefile = handler.get_remote_file("def")
            delete_mock.assert_any_call(remotefile)
            delete_mock.assert_any_call(remotefile.replace(".tfplan", ".log"))


class TestPostPlan:
    def test_uploads_and_cleans_up(self, handler_with_bucket, tmp_path):
        handler, _ = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        planfile.write_text("plan")
        definition = MagicMock()
        definition.plan_file = str(planfile)
        definition.name = "def"
        result = MagicMock()
        result.exit_code = 2
        result.log_file = MagicMock(side_effect=lambda p: Path(p).write_text("log"))
        with patch.object(handler, "_s3_put_plan", return_value=True) as put_mock:
            handler._post_plan(definition, result)
            remotefile = handler.get_remote_file("def")
            put_mock.assert_any_call(planfile, remotefile)
            put_mock.assert_any_call(
                planfile.with_suffix(".log"), remotefile.replace(".tfplan", ".log")
            )
        assert not planfile.with_suffix(".log").exists()


class TestCheckPlan:
    def test_existing_nonzero_plan_raises(self, handler_with_bucket, tmp_path):
        handler, _ = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        planfile.write_text("data")
        definition = MagicMock()
        definition.name = "def"
        definition.plan_file = str(planfile)
        with pytest.raises(HandlerError):
            handler._check_plan("dep", definition)

    def test_zero_size_plan_removed(self, handler_with_bucket, tmp_path):
        handler, _ = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        planfile.write_text("")
        definition = MagicMock()
        definition.name = "def"
        definition.plan_file = str(planfile)
        handler._check_plan("dep", definition)
        assert not planfile.exists()

    def test_downloaded_bad_lineage_removed(
        self, handler_with_bucket, tmp_path, monkeypatch
    ):
        handler, _ = handler_with_bucket
        planfile = tmp_path / "plan.tfplan"
        definition = MagicMock()
        definition.name = "def"
        definition.plan_file = str(planfile)

        def fake_get(local, remote):
            Path(local).write_text("data")
            return True

        monkeypatch.setattr(handler, "_s3_get_plan", fake_get)
        monkeypatch.setattr(handler, "_verify_lineage", lambda p, s: False)
        with patch.object(handler, "_s3_delete_plan") as del_mock:
            handler._check_plan("dep", definition)
            del_mock.assert_called_once()
        assert not planfile.exists()
