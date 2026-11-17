"""Login for JuteTransfer: the user's own VoWERP e-mail and password, plus the
app's access list (see access.py for the roles and the transition switch)."""

from typing import Optional

import streamlit as st

from . import access


def initialize_session_state():
    """Initialize session state variables for authentication."""
    for key, default in (("authenticated", False), ("username", None),
                         ("user_id", None), ("role", None)):
        if key not in st.session_state:
            st.session_state[key] = default


def _sign_in(user_id: int, name: str, role: str) -> None:
    st.session_state.authenticated = True
    st.session_state.username = name
    st.session_state.user_id = user_id
    st.session_state.role = role


def _try_login(username: str, password: str) -> Optional[str]:
    """Sign in, or return the message to show."""
    if access.legacy_login_enabled() and username in access.LEGACY_USERS:
        pw, role = access.LEGACY_USERS[username]
        if password == pw:
            _sign_in(1, username, role)  # shared login: the old fixed user id
            return None
        return "Wrong e-mail or password."
    try:
        who = access.authenticate(username, password)
    except Exception as e:
        return f"Could not check the login: {e}"
    if who is None:
        return "Wrong e-mail or password."
    if who["role"] is None:
        return ("Your VoWERP login is correct, but you do not have access to the "
                "transfer app yet. Ask a transfer admin to add you.")
    _sign_in(who["user_id"], who["name"], who["role"])
    return None


def login() -> bool:
    """Display login form and handle authentication."""
    initialize_session_state()

    if st.session_state.authenticated:
        return True

    st.title("🔐 JuteTransfer Login")
    st.write("Log in with your VoWERP e-mail and password.")

    with st.form("login_form"):
        username = st.text_input("E-mail")
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Login")

        if submit:
            problem = _try_login(username.strip(), password)
            if problem:
                st.error(problem)
            else:
                st.rerun()

    return st.session_state.authenticated


def logout():
    """Handle user logout."""
    st.session_state.authenticated = False
    st.session_state.username = None
    st.session_state.user_id = None
    st.session_state.role = None
    st.rerun()


def is_authenticated() -> bool:
    """Check if user is authenticated."""
    return st.session_state.get("authenticated", False)


def get_username() -> Optional[str]:
    """Get the current username."""
    return st.session_state.get("username")


def get_role() -> Optional[str]:
    """Role of the logged-in user ('user' / 'admin')."""
    return st.session_state.get("role")


def is_admin() -> bool:
    """May the logged-in user change or delete saved transfers? Only decides
    what the pages offer; the ops functions check again (access.require_admin)."""
    return st.session_state.get("role") == access.ROLE_ADMIN
