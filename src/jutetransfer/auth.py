"""Authentication module for JuteTransfer."""

import streamlit as st
from typing import Optional


def initialize_session_state():
    """Initialize session state variables for authentication."""
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "username" not in st.session_state:
        st.session_state.username = None


def check_credentials(username: str, password: str) -> bool:
    """
    Check if provided credentials are valid.
    
    Args:
        username: The username to check
        password: The password to check
    
    Returns:
        True if credentials are valid, False otherwise
    """
    # Simple hardcoded credentials for demo purposes
    # In production, use proper authentication with hashed passwords
    valid_users = {
        "admin": "admin123",
        "user": "user123"
    }
    return username in valid_users and valid_users[username] == password


def login() -> bool:
    """
    Display login form and handle authentication.
    
    Returns:
        True if user is authenticated, False otherwise
    """
    initialize_session_state()
    
    if st.session_state.authenticated:
        return True
    
    st.title("🔐 JuteTransfer Login")
    st.write("Please enter your credentials to access the application.")
    
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Login")
        
        if submit:
            if check_credentials(username, password):
                st.session_state.authenticated = True
                st.session_state.username = username
                st.success(f"Welcome, {username}!")
                st.rerun()
            else:
                st.error("Invalid username or password")
    
    # Display demo credentials
    with st.expander("Demo Credentials"):
        st.write("**Username:** admin | **Password:** admin123")
        st.write("**Username:** user | **Password:** user123")
    
    return st.session_state.authenticated


def logout():
    """Handle user logout."""
    st.session_state.authenticated = False
    st.session_state.username = None
    st.rerun()


def is_authenticated() -> bool:
    """Check if user is authenticated."""
    return st.session_state.get("authenticated", False)


def get_username() -> Optional[str]:
    """Get the current username."""
    return st.session_state.get("username")
