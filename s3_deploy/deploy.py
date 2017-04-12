
import os
import re
import argparse
import logging
import gzip
import shutil
import json
import mimetypes
from datetime import datetime


import boto3
from botocore.client import ClientError

from six import BytesIO

from . import config
from . import github
from .prefixcovertree import PrefixCoverTree

# Support UTC timezone in 2.7
try:
    from datetime import timezone
    UTC = timezone.utc
except ImportError:
    from datetime import tzinfo, timedelta

    class UTCTz(tzinfo):
        def utcoffset(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return 'UTC'

        def dst(self, dt):
            return timedelta(0)

    UTC = UTCTz()


COMPRESSED_EXTENSIONS = frozenset([
    '.txt', '.html', '.css', '.js', '.json', '.xml', '.rss'])

_STORAGE_STANDARD = 'STANDARD'
_STORAGE_REDUCED_REDUDANCY = 'REDUCED_REDUNDANCY'

logger = logging.getLogger(__name__)

mimetypes.init()


def key_name_from_path(path):
    """Convert a relative path into a key name."""
    key_parts = []
    while True:
        head, tail = os.path.split(path)
        if tail != '.':
            key_parts.append(tail)
        if head == '':
            break
        path = head

    return '/'.join(reversed(key_parts))


def upload_key(obj, path, cache_rules, dry, storage_class=None):
    """Upload data in path to key."""

    mime_guess = mimetypes.guess_type(obj.key)
    if mime_guess is not None:
        content_type = mime_guess[0]
    else:
        content_type = 'application/octet-stream'

    content_file = open(path, 'rb')
    try:
        encoding = None

        cache_control = config.resolve_cache_rules(obj.key, cache_rules)
        if cache_control is not None:
            logger.debug('Using cache control: {}'.format(cache_control))

        _, ext = os.path.splitext(path)
        if ext in COMPRESSED_EXTENSIONS:
            logger.info('Compressing {}...'.format(obj.key))
            compressed = BytesIO()
            gzip_file = gzip.GzipFile(
                fileobj=compressed, mode='wb', compresslevel=9)
            try:
                shutil.copyfileobj(content_file, gzip_file)
            finally:
                gzip_file.close()
            compressed.seek(0)
            content_file, _ = compressed, content_file.close()  # noqa
            encoding = 'gzip'

        logger.info('Uploading {}...'.format(obj.key))

        if not dry:
            kwargs = {}
            if content_type is not None:
                kwargs['ContentType'] = content_type
            if cache_control is not None:
                kwargs['CacheControl'] = cache_control

            if encoding is not None:
                kwargs['ContentEncoding'] = encoding

            if storage_class is not None:
                kwargs['StorageClass'] = storage_class

            obj.put(Body=content_file.read(), **kwargs)
    finally:
        content_file.close()


def get_s3_bucket(bucket_name, s3):
    """"
    Takes the s3 and bucket_name and returns s3 bucket
    If does not exist, it will create bucket with permissions
    """
    bucket = s3.Bucket(bucket_name)
    exists = True
    try:
        s3.meta.client.head_bucket(Bucket=bucket_name)
    except ClientError as e:
        # If a client error is thrown, then check that it was a 404 error.
        # If it was a 404 error, then the bucket does not exist.
        error_code = int(e.response['Error']['Code'])
        if error_code == 404:
            exists = False
    if exists is False:
        s3.create_bucket(Bucket=bucket_name, ACL='public-read')

        # We need to set an S3 policy for our bucket to
        # allow anyone read access to our bucket and files.
        # If we do not set this policy, people will not be
        # able to view our S3 static web site.
        bucket_policy = s3.BucketPolicy(bucket_name)
        policy_payload = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "Allow Public Access to All Objects",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": "arn:aws:s3:::%s/*" % (bucket_name)
            }]
        }
        # Add the policy to the bucket
        bucket_policy.put(Policy=json.dumps(policy_payload))
        # Make our new S3 bucket a static website
        bucket_website = s3.BucketWebsite(bucket_name)
        # Create the configuration for the website
        website_configuration = {
            'ErrorDocument': {'Key': 'error.html'},
            'IndexDocument': {'Suffix': 'index.html'},
        }
        bucket_website.put(
            WebsiteConfiguration=website_configuration
        )
        bucket = s3.Bucket(bucket_name)

    return bucket


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('boto3').setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description='AWS S3 website deployment tool')
    parser.add_argument(
        '-f', '--force', action='store_true', dest='force',
        help='force upload of all files')
    parser.add_argument(
        '-n', '--dry-run', action='store_true', dest='dry',
        help='run without uploading any files')
    parser.add_argument(
        'path', help='the .s3_website.yaml configuration file or directory',
        default='.', nargs='?')
    args = parser.parse_args()
    path = args.path
    PR_NUMBER = os.environ.get('TRAVIS_PULL_REQUEST')
    REPO_SLUG = os.environ.get('TRAVIS_REPO_SLUG')
    TOKEN = os.environ.get('TRAVIS_BOT_GITHUB_TOKEN')
    branch_name = os.environ.get('TRAVIS_PULL_REQUEST_BRANCH')
    SITE_LOCATION = os.environ.get('SITE_LOCATION')

    if PR_NUMBER == "false":
        print('Not a pull request, not running')
        sys.exit(0)

    # Open configuration file
    site_stub = REPO_SLUG.replace('/', '-').replace('.', '-')
    base_path = os.path.dirname(path)
    site_name = site_stub + '-' + branch_name
    conf = {
        's3_bucket': site_name
        }
    if not SITE_LOCATION:
        conf['site'] = '_book'
    else:
        conf['site'] = SITE_LOCATION

    bucket_name = conf['s3_bucket']
    cache_rules = conf.get('cache_rules', [])
    if conf.get('s3_reduced_redundancy', False):
        storage_class = _STORAGE_REDUCED_REDUDANCY
    else:
        storage_class = _STORAGE_STANDARD

    logger.info('Connecting to bucket {}...'.format(bucket_name))

    s3 = boto3.resource('s3')

    bucket = get_s3_bucket(bucket_name, s3)

    site_dir = os.path.join(base_path, conf['site'])

    logger.info('Site: {}'.format(site_dir))

    processed_keys = set()
    updated_keys = set()

    for obj in bucket.objects.all():
        processed_keys.add(obj.key)
        path = os.path.join(site_dir, obj.key)

        # Delete keys that have been deleted locally
        if not os.path.isfile(path):
            logger.info('Deleting {}...'.format(obj.key))
            if not args.dry:
                obj.delete()
            updated_keys.add(obj.key)
            continue

        # Skip keys that have not been updated
        mtime = datetime.fromtimestamp(os.path.getmtime(path), UTC)
        if not args.force:
            if (mtime <= obj.last_modified and
                    obj.storage_class == storage_class):
                logger.info('Not modified, skipping {}.'.format(obj.key))
                continue

        upload_key(
            obj, path, cache_rules, args.dry, storage_class=storage_class)
        updated_keys.add(obj.key)

    for dirpath, dirnames, filenames in os.walk(site_dir):
        key_base = os.path.relpath(dirpath, site_dir)
        for name in filenames:
            path = os.path.join(dirpath, name)
            key_name = key_name_from_path(os.path.join(key_base, name))
            if key_name in processed_keys:
                continue

            # Create new object
            obj = bucket.Object(key_name)

            logger.info('Creating key {}...'.format(obj.key))

            upload_key(
                obj, path, cache_rules, args.dry, storage_class=storage_class)
            updated_keys.add(key_name)

    logger.info('Bucket update done.')

    comment = github.build_comment(site_name)
    response = github.comment_on_pull_request(
        PR_NUMBER, REPO_SLUG, TOKEN, comment)

    # Invalidate files in cloudfront distribution
    if 'cloudfront_distribution_id' in conf:
        logger.info('Connecting to Cloudfront distribution {}...'.format(
            conf['cloudfront_distribution_id']))

        index_pattern = None
        if 'index_document' in conf:
            index_doc = conf['index_document']
            index_pattern = r'(^(?:.*/)?)' + re.escape(index_doc) + '$'

        def path_from_key_name(key_name):
            if index_pattern is not None:
                m = re.match(index_pattern, key_name)
                if m:
                    return m.group(1)
            return key_name

        t = PrefixCoverTree()
        for key_name in updated_keys:
            t.include(path_from_key_name(key_name))
        for key_name in processed_keys - updated_keys:
            t.exclude(path_from_key_name(key_name))

        paths = []
        for prefix, exact in t.matches():
            path = '/' + prefix + ('' if exact else '*')
            logger.info('Preparing to invalidate {}...'.format(path))
            paths.append(path)

        cloudfront = boto3.client('cloudfront')

        if len(paths) > 0:
            dist_id = conf['cloudfront_distribution_id']
            if not args.dry:
                logger.info('Creating invalidation request...')
                response = cloudfront.create_invalidation(
                    DistributionId=dist_id,
                    InvalidationBatch=dict(
                        Paths=dict(
                            Quantity=len(paths),
                            Items=paths
                        ),
                        CallerReference='s3-deploy-website'
                    )
                )
                invalidation = response['Invalidation']
                logger.info('Invalidation request {} is {}'.format(
                    invalidation['Id'], invalidation['Status']))
        else:
            logger.info('Nothing updated, invalidation skipped.')
