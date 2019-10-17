"""
Utils for dealing with hubploy config
"""
import os
from ruamel.yaml import YAML
from repo2docker.app import Repo2Docker
import docker

from . import gitutils
yaml = YAML(typ='safe')

class LocalImage:
    def __init__(self, name, path, helm_substitution_path='jupyterhub.singleuser.image'):
        """
        Create an Image from a local path

        name: Fully qualified name of image
        path: Absolute path to local directory with image contents
        helm_substitution_path: Dot separated path in a helm file that should be populated with this image spec
        """
        self.name = name

        self.tag = gitutils.last_modified_commit(path)
        if not self.tag:
            # Suport uncommitted images locally
            # FIXME: Emit a warning here?
            self.tag = 'latest'
        self.path = path
        self.helm_substitution_path = helm_substitution_path
        self.image_spec = f'{self.name}:{self.tag}'

        # Make r2d object here so we can use it to build & push
        self.r2d = Repo2Docker()
        self.r2d.subdir = self.path
        self.r2d.output_image_spec = self.image_spec
        self.r2d.user_id = 1000
        self.r2d.user_name = 'jovyan'
        self.r2d.target_repo_dir = '/srv/repo'
        self.r2d.initialize()


    @property
    def docker(self):
        """
        Return a shared docker client object

        Creating a docker client object with automatic version
        selection can be expensive (since there needs to be an API
        request to determien version). So we cache it on a per-class
        level.
        """
        # FIXME: Is this racey?
        if not hasattr(self.__class__, '_docker'):
            self.__class__._docker = docker.from_env()

        return self.__class__._docker

    def exists_in_registry(self):
        """
        Return true if image exists in registry
        """
        try:
            image_manifest = self.docker.images.get_registry_data(self.image_spec)
            return image_manifest is not None
        except docker.errors.ImageNotFound:
            return False
        except docker.errors.APIError as e:
            # This message seems to vary across registries?
            if e.explanation.startswith('manifest unknown: '):
                return False
            else:
                raise

    def get_possible_parent_tags(self, n=16):
        """
        List n possible image tags that might be the same image built previously.

        It is much faster to build a new image if we have a list of cached
        images that were built from the same source. This forces a rebuild of
        only the parts that have changed.

        Since we know how the tags are formed, we try to find upto n tags for
        this image that might be possible cache hits
        """
        for i in range(1, n):
            # FIXME: Make this look for last modified since before beginning of commit_range
            # Otherwise, if there are more than n commits in the current PR that touch this
            # local image, we might not get any useful caches
            yield gitutils.last_modified_commit(self.path, n=i)

    def fetch_parent_image(self):
        """
        Prime local image cache by pulling possible parent images.

        Return spec of parent image, or None if no parents could be pulled
        """
        for tag in self.get_possible_parent_tags():
            parent_image_spec = f'{self.name}:{tag}'
            try:
                print(f'Trying to fetch parent image {parent_image_spec}')
                self.docker.images.pull(parent_image_spec)
                return parent_image_spec
            except docker.errors.NotFound:
                pass
        return None

    def needs_building(self, check_registry=False, commit_range=None):
        """
        Return true if image needs to be built.

        One of check_registry or commit_range must be set
        """
        if check_registry and commit_range:
            raise ValueError("Only one of check_registry or commit_range can be set")

        if not (check_registry or commit_range):
            raise ValueError("One of check_registry or commit_range must be set")

        if check_registry:
            return not self.exists_in_registry()

        if commit_range:
            return gitutils.path_touched(self.path, commit_range=commit_range)


    def build(self):
        """
        Build local image with repo2docker
        """
        parent_image_spec = self.fetch_parent_image()
        if parent_image_spec:
            self.r2d.cache_from = [parent_image_spec]

        self.r2d.build()

    def push(self):
        self.r2d.push_image()



def get_config(deployment):
    """
    Return configuration if it exists for a deployment

    Normalize images config if it exists
    """
    deployment_path = os.path.abspath(os.path.join('deployments', deployment))
    config_path = os.path.join(deployment_path, 'hubploy.yaml')

    if not os.path.exists(config_path):
        return {}
    
    with open(config_path) as f:
        config = yaml.load(f)

    if 'images' in config:
        images_config = config['images']

        if 'image_name' in images_config:
            # Only one image is being built
            # FIXME: Deprecate after moving other hubploy users to list format
            images = [{
                'name': images_config['image_name'],
                'path': 'image',
                'helm_substitution_path': images_config['image_config_path']
            }]
        else:
            # Multiple images are being built
            images = images_config['images']

    for image in images:
        # Normalize paths to be absolute paths
        image['path'] = os.path.join(deployment_path, image['path'])

    config['images']['images'] = [LocalImage(**i) for i in images]
    
    return config
    