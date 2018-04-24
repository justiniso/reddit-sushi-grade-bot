import json

import boto3

class RemoteFileCache:
    """Remote file-based cache backed by s3"""

    def __init__(self, bucket, path):
        self.s3 = boto3.resource('s3')
        self.bucket = bucket
        self.path = path

    def read(self) -> dict:
        try:
            data = self.s3.Object(self.bucket, self.path).get()['Body'].read()
        except self.s3.meta.client.exceptions.NoSuchKey:
            return {}

        return json.loads(data)

    def get(self, key):
        return self.read().get(key)

    def set(self, key, value) -> dict:
        data = self.read()
        data[key] = value
        self.s3.Object(self.bucket, self.path).put(Body=json.dumps(data))
        return data