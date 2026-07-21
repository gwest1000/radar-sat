#!/usr/bin/env python3
from __future__ import annotations

import argparse

from radarsat.r2 import R2Config, boto3_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure Radar-Sat R2 CORS and the retention backstop."
    )
    parser.add_argument(
        "--site-origin",
        action="append",
        default=[],
        help="Allowed browser origin; may be repeated.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    origins = args.site_origin or [
        "https://gwest1000.github.io",
        "http://localhost:3000",
    ]
    config = R2Config.from_environment()
    client = boto3_client(config)
    client.head_bucket(Bucket=config.bucket)
    client.put_bucket_cors(
        Bucket=config.bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedMethods": ["GET", "HEAD"],
                    "AllowedOrigins": origins,
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": [
                        "ETag",
                        "Content-Length",
                        "Content-Type",
                        "Last-Modified",
                    ],
                    "MaxAgeSeconds": 86400,
                }
            ]
        },
    )
    client.put_bucket_lifecycle_configuration(
        Bucket=config.bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-observational-frames",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "frames/"},
                    "Expiration": {"Days": 9},
                },
                {
                    "ID": "expire-observational-metadata",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "metadata/"},
                    "Expiration": {"Days": 9},
                },
                {
                    "ID": "abort-incomplete-uploads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                },
            ]
        },
    )
    print(
        f"Configured CORS and 9-day lifecycle backstop on R2 bucket {config.bucket}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
