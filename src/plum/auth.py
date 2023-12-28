from collections.abc import Callable
from functools import wraps
from sqlite3 import Row
from typing import ParamSpec, TypedDict, TypeVar

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash
from werkzeug.wrappers.response import Response

from .db import get_db

# Constants
AUTH_SESSION_KEY = "__plum_auth"


class ClassDict(TypedDict):
    class_id: int
    class_name: str
    role: str


class AuthDict(TypedDict, total=False):
    user_id: int | None
    display_name: str | None
    is_admin: bool
    is_tester: bool
    class_id: int | None
    class_name: str | None
    role_id: int | None
    role: str | None
    auth_provider: str
    other_classes: list[ClassDict]


def set_session_auth(user_id: int, display_name: str, is_admin: bool = False, is_tester: bool = False, role_id: int | None = None) -> None:
    auth: AuthDict = {
        'user_id': user_id,
        'display_name': display_name,
        'is_admin': is_admin,
        'is_tester': is_tester,
        'role_id': role_id,
    }
    session[AUTH_SESSION_KEY] = auth


def set_session_auth_role(role_id: int) -> None:
    auth: AuthDict = session[AUTH_SESSION_KEY]
    auth['role_id'] = role_id
    session[AUTH_SESSION_KEY] = auth


def _get_auth_from_session() -> AuthDict:
    base: AuthDict = {
        'user_id': None,
        'display_name': None,
        'is_admin': False,
        'is_tester': False,
        'class_id': None,
        'class_name': None,
        'role_id': None,
        'role': None,
    }
    # Get the session auth dict, or an empty dict if it's not there, then
    # "override" any values in 'base' that are defined in the session auth dict.
    auth_dict: AuthDict = base | session.get(AUTH_SESSION_KEY, {})

    db = get_db()

    if auth_dict['user_id']:
        # Get the auth provider
        provider_row = db.execute("""
            SELECT auth_providers.name
            FROM users
            LEFT JOIN auth_providers ON auth_providers.id=users.auth_provider
            WHERE users.id=?
        """, [auth_dict['user_id']]).fetchone()

        if not provider_row:
            # fall-through if user_id is not in database (deleted from DB?)
            return base

        auth_dict['auth_provider'] = provider_row['name']

        # Check the database for any active roles (may be changed by another user)
        # and populate class/role information.
        # Uses WHERE active=1 to only allow active roles.
        role_rows = db.execute("""
            SELECT
                roles.id,
                roles.class_id,
                classes.name,
                classes.enabled,
                roles.role
            FROM roles
            JOIN classes ON classes.id=roles.class_id
            WHERE roles.user_id=? AND roles.active=1
            ORDER BY roles.id DESC
        """, [auth_dict['user_id']]).fetchall()
        if role_rows:
            auth_dict['other_classes'] = []  # for storing active classes that are not the user's currently chosen class
            for row in role_rows:
                class_dict: ClassDict = {
                    'class_id': row['class_id'],
                    'class_name': row['name'],
                    'role': row['role'],
                }
                if row['id'] == auth_dict['role_id']:
                    # set values for the current role
                    auth_dict['class_id'] = class_dict['class_id']
                    auth_dict['class_name'] = class_dict['class_name']
                    auth_dict['role'] = class_dict['role']
                elif row['enabled']:
                    auth_dict['other_classes'].append(class_dict)
        else:
            # ensure we don't keep a role_id
            auth_dict['role_id'] = None

    return auth_dict


def get_auth() -> AuthDict:
    if 'auth' not in g:
        g.auth = _get_auth_from_session()

    return g.auth


def get_last_role(user_id: int) -> int | None:
    """ Find and return the last role (as a role ID) for the given user,
        as long as that role still exists and is currently active.

        Returns the role_id or None if nothing is found / matches.
    """
    db = get_db()

    role_row = db.execute("""
        SELECT roles.id AS role_id
        FROM roles
        JOIN users ON roles.user_id=users.id
        WHERE users.id=?
          AND users.last_role_id=roles.id
          AND roles.active=1
    """, [user_id]).fetchone()

    if not role_row:
        return None

    role_id = role_row['role_id']
    assert isinstance(role_id, int)
    return role_id


def ext_login_update_or_create(provider_name: str, user_normed: dict[str, str | None], query_tokens: int=0) -> Row:
    """
    For an external authentication login:
      1. Create an account for the user if they do not already have an account (entry in users)
      2. Update the account with user info provided if one does already exist
      3. Get and return the account info for that user

    Parameters
    ----------
    provider_name : str
      Name of the external auth provider: in set {lti, google, github, microsoft}
    user_normed : dict
      User information.
      Must contain non-null 'ext_id' key; must contain keys 'email', 'full_name', and 'auth_name', and at least one should be non-null.
    query_tokens : int (default 0)
      Number of query tokens to assign to the user *if* creating an account for them (on first login).

    Returns
    -------
    SQLite row object containing the 'users' table row for the now-logged-in user.
    """
    db = get_db()

    provider_row = db.execute("SELECT id FROM auth_providers WHERE name=?", [provider_name]).fetchone()
    provider_id = provider_row['id']

    auth_row = db.execute("SELECT * FROM auth_external WHERE auth_provider=? AND ext_id=?", [provider_id, user_normed['ext_id']]).fetchone()

    if auth_row:
        user_id = auth_row['user_id']
        # Update w/ latest user info (name, email, etc. could conceivably change)
        cur = db.execute(
            "UPDATE users SET full_name=?, email=?, auth_name=? WHERE id=?",
            [user_normed['full_name'], user_normed['email'], user_normed['auth_name'], user_id]
        )
        db.commit()

    else:
        # Create a new user account.
        cur = db.execute(
            "INSERT INTO users (auth_provider, full_name, email, auth_name, query_tokens) VALUES (?, ?, ?, ?, ?)",
            [provider_id, user_normed['full_name'], user_normed['email'], user_normed['auth_name'], query_tokens]
        )
        user_id = cur.lastrowid
        db.execute("INSERT INTO auth_external(user_id, auth_provider, ext_id) VALUES (?, ?, ?)", [user_id, provider_id, user_normed['ext_id']])
        db.commit()

    # get all values in newly updated/inserted row
    user_row = db.execute("SELECT * FROM users WHERE id=?", [user_id]).fetchone()
    assert isinstance(user_row, Row)
    return user_row


bp = Blueprint('auth', __name__, url_prefix="/auth", template_folder='templates')


@bp.route("/login", methods=['GET', 'POST'])
def login() -> str | Response:
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        db = get_db()
        auth_row = db.execute("SELECT * FROM auth_local JOIN users ON auth_local.user_id=users.id WHERE username=?", [username]).fetchone()

        if not auth_row or not check_password_hash(auth_row['password'], password):
            flash("Invalid username or password.", "warning")
        else:
            # Success!
            last_role_id = get_last_role(auth_row['id'])
            set_session_auth(auth_row['id'], auth_row['display_name'], is_admin=auth_row['is_admin'], is_tester=auth_row['is_tester'], role_id=last_role_id)
            next_url = request.form['next'] or url_for("helper.help_form")
            return redirect(next_url)

    # we either have a GET request or we fell through the POST login attempt with a failure
    next_url = request.args.get('next', '')
    return render_template("login.html", next_url=next_url)


@bp.route("/logout", methods=['POST'])
def logout() -> Response:
    session.clear()  # clear the entire session to be safest here.
    flash("You have been logged out.")
    return redirect(url_for(".login"))


# For decorator type hints
P = ParamSpec('P')
R = TypeVar('R')


def login_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    '''Redirect to login on this route if user is not logged in.'''
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if not auth['user_id']:
            flash("Login required.", "warning")
            return redirect(url_for('auth.login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def instructor_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if auth['role'] != "instructor":
            return abort(403)
        return f(*args, **kwargs)
    return decorated_function


def class_enabled_required(f: Callable[P, R]) -> Callable[P, str | R]:
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> str | R:
        auth = get_auth()
        class_id = auth['class_id']

        if class_id is None:
            # No active class, no problem
            return f(*args, **kwargs)

        # Otherwise, there's an active class, so we require it to be enabled.
        db = get_db()
        class_row = db.execute("SELECT * FROM classes WHERE id=?", [class_id]).fetchone()
        if not class_row['enabled']:
            flash("The current class is archived or disabled.  New requests cannot be made.", "warning")
            return render_template("error.html")

        return f(*args, **kwargs)

    return decorated_function


def admin_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    '''Redirect to login on this route if user is not an admin.'''
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if not auth['is_admin']:
            flash("Login required.", "warning")
            return redirect(url_for('auth.login', next=request.full_path))
        return f(*args, **kwargs)
    return decorated_function


def tester_required(f: Callable[P, R]) -> Callable[P, Response | R]:
    '''Return a 404 on this route (hide it, basically) if user is not a tester.'''
    @wraps(f)
    def decorated_function(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        auth = get_auth()
        if not auth['is_tester']:
            return abort(404)
        return f(*args, **kwargs)
    return decorated_function
