# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import datetime
import enum
import logging

from flask import current_app

from landoapi import repos
from landoapi.models.patch import Patch
from landoapi.storage import db
from landoapi.transplant_client import TransplantClient, TransplantError

logger = logging.getLogger(__name__)


@enum.unique
class LandingStatus(enum.Enum):
    """Status of the landing request."""
    # Default value - stays in database only if landing request was aborted.
    aborted = 'aborted'

    # Set from pingback
    submitted = 'submitted'
    landed = 'landed'
    failed = 'failed'


class Landing(db.Model):
    """Represents the landing process in Autoland.

    Landing is communicating with Autoland via TransplantClient.
    Landing is communicating with Phabricator via PhabricatorClient.
    Landing object might be saved to database without creation of the actual
    landing in Autoland. It is done before landing request to construct
    required "pingback URL" and save related Patch objects.
    To update the Landing status Transplant is calling provided pingback URL.
    Active Diff Id is stored on creation if it is different than diff_id.

    Attributes:
        id: Primary Key
        request_id: Id of the request in Autoland
        revision_id: Phabricator id of the revision to be landed
        diff_id: Phabricator id of the diff to be landed
        active_diff_id: Phabricator id of the diff active at the moment of
            landing
        status: Status of the landing. Modified by `update` API
        error: Text describing the error if not landed
        result: Revision (sha) of push
        requester_email: The email address of the requester of the landing.
        tree: The treestatus tree name the revision is to land to.
        created_at: DateTime of the creation
        updated_at: DateTime of the last save
    """
    __tablename__ = "landings"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, unique=True)
    revision_id = db.Column(db.Integer)
    diff_id = db.Column(db.Integer)
    active_diff_id = db.Column(db.Integer)
    status = db.Column(
        db.Enum(LandingStatus), nullable=False, default=LandingStatus.aborted
    )
    error = db.Column(db.Text(), default='')
    result = db.Column(db.Text(), default='')
    requester_email = db.Column(db.String(254))
    tree = db.Column(db.String(128))
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=db.func.now()
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=db.func.now(),
        onupdate=db.func.now()
    )

    @classmethod
    def create(cls, revision, diff_id, requester_email, phab, active_diff_id):
        """Land revision.

        A typical successful story:
            * Revision and Diff are loaded from Phabricator.
            * Patch is created and uploaded to S3 bucket.
            * Landing object is created (without request_id)
            * A request to land the patch is send to Transplant client.
            * Created landing object is updated with returned `request_id`
              and status `submitted`. It is then saved and returned.

        Args:
            revision: A dict of the revision data just as it is returned
                by Phabricator.
            diff_id: The id of the diff to be landed
            requester_email: The LDAP email address of the person requesting
                the landing
            phab: The PhabricatorClient instance to use
            active_diff_id: The diff id of the latest diff for the given
                revision. Not always equal to diff_id.

        Returns:
            A new Landing object

        Raises:
            LandingNotCreatedException: landing request in Transplant failed
        """
        assert revision is not None
        revision_id = int(revision['id'])

        if 'repositoryPHID' not in revision or not revision['repositoryPHID']:
            raise InvalidRepositoryException(
                'This revision is not associated with a repository. '
                'Associate the revision with a repository on Phabricator then'
                'try again.'
            )

        # Map the Phabricator repo to the treestatus tree
        repo = phab.call_conduit(
            'diffusion.repository.search',
            constraints={'phids': [revision['repositoryPHID']]}
        )
        repo_short_name = phab.expect(repo, 'data', 0, 'fields', 'shortName')
        if repo_short_name not in repos.REPO_CONFIG:
            raise InvalidRepositoryException(
                'Landing to {} is not supported at this time. '.
                format(repo_short_name)
            )

        target_repo = repos.REPO_CONFIG[repo_short_name]
        tree = target_repo['tree']

        # Save the initial landing request
        landing = cls(
            diff_id=diff_id,
            active_diff_id=active_diff_id,
            revision_id=revision_id,
            requester_email=requester_email,
            tree=tree
        )
        db.session.add(landing)
        db.session.commit()

        # Create a patch and upload it to S3
        patch = Patch(landing.id, revision, diff_id)
        patch.upload(phab)

        # Send the request to transplant for landing
        trans = TransplantClient(
            current_app.config['TRANSPLANT_URL'],
            current_app.config['TRANSPLANT_USERNAME'],
            current_app.config['TRANSPLANT_PASSWORD'],
        )
        try:
            request_id = trans.land(
                revision_id=revision_id,
                ldap_username=landing.requester_email,
                patch_urls=[patch.s3_url],
                tree=tree,
                pingback=current_app.config['PINGBACK_URL'],
                push_bookmark=target_repo['push_bookmark']
            )
        except TransplantError:
            raise LandingNotCreatedException

        if not request_id:
            raise LandingNotCreatedException

        landing.request_id = request_id
        landing.status = LandingStatus.submitted
        db.session.commit()

        logger.info(
            {
                'revision_id': revision_id,
                'landing_id': landing.id,
                'msg': 'landing created for revision'
            }, 'landing.success'
        )

        return landing

    @classmethod
    def is_revision_submitted(cls, revision_id):
        """Check if revision is successfully submitted.

        Args:
            revision_id: The integer id of the revision.

        Returns:
            Landed Revision object or False if not submitted.
        """
        landings = cls.query.filter(
            cls.revision_id == revision_id,
            cls.status == LandingStatus.submitted
        ).all()
        if not landings:
            return False

        return landings[0]

    @classmethod
    def latest_landed(cls, revision_id):
        """Return the latest Landing that is landed, or None.

        Args:
            revision_id: The integer id of the revision.

        Returns:
            Latest landing object with status landed, or None if
            none exist.
        """
        return cls.query.filter_by(
            revision_id=revision_id, status=LandingStatus.landed
        ).order_by(cls.updated_at.desc()).first()

    def __repr__(self):
        return '<Landing: %s>' % self.id

    def serialize(self):
        """Serialize to JSON compatible dictionary."""
        return {
            'id': self.id,
            'revision_id': 'D{}'.format(self.revision_id),
            'request_id': self.request_id,
            'diff_id': self.diff_id,
            'active_diff_id': self.active_diff_id,
            'status': self.status.value,
            'error_msg': self.error,
            'result': self.result,
            'requester_email': self.requester_email,
            'tree': self.tree,
            'created_at': (
                self.created_at.astimezone(datetime.timezone.utc).isoformat()
            ),
            'updated_at': (
                self.updated_at.astimezone(datetime.timezone.utc).isoformat()
            ),
        }  # yapf: disable

    def update_from_transplant(self, landed, error='', result=''):
        """Set the status from pingback request."""
        self.error = error
        self.result = result
        if not landed:
            self.status = (
                LandingStatus.failed if error else LandingStatus.submitted
            )
        else:
            self.status = LandingStatus.landed


class LandingNotCreatedException(Exception):
    """Transplant service failed to land a revision."""
    pass


class InvalidRepositoryException(Exception):
    """The target landing repository is invalid."""
    pass
