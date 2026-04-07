"""Main Streamlit application for JuteTransfer."""

import streamlit as st
import pandas as pd
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode
from src.jutetransfer.auth import login, logout, is_authenticated, get_username

from src.jutetransfer.pages import jute_mr_table_page, schema_viewer_page, transfer_chain_page


def configure_page():
    """Configure Streamlit page settings."""
    st.set_page_config(
        page_title="JuteTransfer",
        page_icon="🌾",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def display_sidebar():
    """Display sidebar with user information and navigation."""
    with st.sidebar:
        st.title("🌾 JuteTransfer")
        st.markdown("---")

        if is_authenticated():
            st.success(f"👤 Logged in as: **{get_username()}**")
            st.markdown("---")

            st.subheader("Navigation")
            page = st.radio(
                "Select Page",
                ["Dashboard", "Analytics",
                    "Jute MR Table", "Transfer Chain (Vertical)", "Schema Viewer"],
                label_visibility="collapsed"
            )

            st.markdown("---")
            if st.button("🚪 Logout", use_container_width=True):
                logout()

            return page

        return None



def display_aggrid(df: pd.DataFrame):
    """Display data using AgGrid with interactive features."""
    st.subheader("📊 Jute Transfer Data Grid")

    # Configure grid options
    gb = GridOptionsBuilder.from_dataframe(df)
    gb.configure_default_column(resizable=True, filterable=True, sortable=True)
    gb.configure_pagination(paginationAutoPageSize=True)
    gb.configure_selection(selection_mode="single", use_checkbox=False)
    grid_options = gb.build()

    # Display AgGrid
    AgGrid(
        df,
        gridOptions=grid_options,
        height=500,
        theme="streamlit",
        update_mode=GridUpdateMode.MODEL_CHANGED,
    )


def dashboard_page():
    """Display dashboard page."""
    st.title("🌾 JuteTransfer Dashboard")
    st.markdown("Welcome to the JuteTransfer Management System")



def analytics_page():
    """Display analytics page."""
    st.title("📈 Analytics")


def main():
    """Main application entry point."""
    configure_page()

    # Check authentication
    if not login():
        return

    # Display sidebar and get selected page
    page = display_sidebar()

    # Display selected page
    if page == "Dashboard":
        dashboard_page()
    elif page == "Analytics":
        analytics_page()
    elif page == "Jute MR Table":
        jute_mr_table_page()
    elif page == "Transfer Chain (Vertical)":
        transfer_chain_page()
    elif page == "Schema Viewer":
        schema_viewer_page()


if __name__ == "__main__":
    main()
