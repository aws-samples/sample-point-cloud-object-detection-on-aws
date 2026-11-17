"""Presigned upload URLs name the bucket's own Region (api/deps.py get_s3_client).

Left to botocore's default, a client for us-east-2 presigns against the global host
<bucket>.s3.amazonaws.com. S3 answers that host for a bucket created outside us-east-1
with 307 Temporary Redirect until its DNS catches up, which can take up to a day, and a
presigned URL cannot follow the redirect: the host is part of what it signs. Browser
uploads then failed after a fresh deployment in any other Region.
"""

import urllib.parse

import pytest

from pointcloud_detect.api import deps

BUCKET = "pointcloud-detection-dev-input-123456789012-us-east-2-an"


@pytest.fixture
def presign(monkeypatch, tmp_path):
    """A presigned upload-part URL from the API's own client."""

    def make(region: str, endpoint: str | None = None) -> urllib.parse.SplitResult:
        monkeypatch.setenv("AWS_REGION", region)
        for name in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        if endpoint:
            monkeypatch.setenv("AWS_ENDPOINT_URL", endpoint)
        # Presigning signs locally and sends nothing; keep any real profile out of it.
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        deps.get_settings.cache_clear()
        deps.get_s3_client.cache_clear()
        try:
            url = deps.get_s3_client().generate_presigned_url(
                "upload_part",
                Params={
                    "Bucket": BUCKET,
                    "Key": "uploads/a/b/scan.ply",
                    "UploadId": "u",
                    "PartNumber": 1,
                },
                ExpiresIn=60,
            )
        finally:
            deps.get_settings.cache_clear()
            deps.get_s3_client.cache_clear()
        return urllib.parse.urlsplit(url)

    return make


@pytest.mark.parametrize("region", ["us-east-2", "eu-west-1", "ap-southeast-2"])
def test_upload_urls_outside_us_east_1_use_the_regional_endpoint(presign, region):
    url = presign(region)
    assert url.scheme == "https"
    assert url.hostname == f"{BUCKET}.s3.{region}.amazonaws.com"
    credential = urllib.parse.parse_qs(url.query)["X-Amz-Credential"][0]
    assert credential.split("/")[2:4] == [region, "s3"]


def test_upload_urls_in_us_east_1_name_a_us_east_1_host(presign):
    # The global host is us-east-1's own, so it never redirects there.
    assert presign("us-east-1").hostname in {
        f"{BUCKET}.s3.amazonaws.com",
        f"{BUCKET}.s3.us-east-1.amazonaws.com",
    }


def test_a_configured_endpoint_keeps_path_style_urls(presign):
    # The compose stack's emulator: virtual hosts under it would not resolve.
    url = presign("us-east-1", "http://localhost:4566")
    assert url.netloc == "localhost:4566"
    assert url.path.startswith(f"/{BUCKET}/")
