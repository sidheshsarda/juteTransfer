"""Main Streamlit application for JuteTransfer."""

import streamlit as st
import pandas as pd
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode
from src.jutetransfer.auth import login, logout, is_authenticated, get_username
from src.jutetransfer.data import generate_sample_data, get_summary_statistics


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
                ["Dashboard", "Data View", "Analytics"],
                label_visibility="collapsed"
            )
            
            st.markdown("---")
            if st.button("🚪 Logout", use_container_width=True):
                logout()
            
            return page
        
        return None


def display_metrics(stats: dict):
    """Display summary metrics in columns."""
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.metric("Total Transfers", stats["Total Transfers"])
        st.metric("Total Quantity", f"{stats['Total Quantity (kg)']:,.0f} kg")
    
    with col2:
        st.metric("Total Cost", f"${stats['Total Cost ($)']:,.2f}")
        st.metric("Avg Quantity", f"{stats['Average Quantity (kg)']:,.2f} kg")
    
    with col3:
        st.metric("Completed", stats["Completed Transfers"])
        st.metric("In Transit", stats["In Transit"])


def display_aggrid(df: pd.DataFrame):
    """Display data using AgGrid with interactive features."""
    st.subheader("📊 Jute Transfer Data Grid")
    
    # Simple AgGrid display
    AgGrid(
        df,
        height=500,
        fit_columns_on_grid_load=True,
        theme="streamlit",
    )


def dashboard_page(df: pd.DataFrame):
    """Display dashboard page."""
    st.title("🌾 JuteTransfer Dashboard")
    st.markdown("Welcome to the JuteTransfer Management System")
    
    # Display summary statistics
    stats = get_summary_statistics(df)
    display_metrics(stats)
    
    st.markdown("---")
    
    # Display charts
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Transfers by Status")
        status_counts = df["Status"].value_counts()
        st.bar_chart(status_counts)
    
    with col2:
        st.subheader("Transfers by Source Location")
        location_counts = df["Source Location"].value_counts()
        st.bar_chart(location_counts)


def data_view_page(df: pd.DataFrame):
    """Display data view page with AgGrid."""
    st.title("📋 Data View")
    
    # Add filter options
    col1, col2 = st.columns(2)
    with col1:
        status_filter = st.multiselect(
            "Filter by Status",
            options=df["Status"].unique(),
            default=df["Status"].unique()
        )
    
    with col2:
        location_filter = st.multiselect(
            "Filter by Source Location",
            options=df["Source Location"].unique(),
            default=df["Source Location"].unique()
        )
    
    # Apply filters
    filtered_df = df[
        (df["Status"].isin(status_filter)) &
        (df["Source Location"].isin(location_filter))
    ]
    
    st.info(f"Showing {len(filtered_df)} of {len(df)} records")
    
    # Display AgGrid
    display_aggrid(filtered_df)


def analytics_page(df: pd.DataFrame):
    """Display analytics page."""
    st.title("📈 Analytics")
    
    tab1, tab2, tab3 = st.tabs(["Overview", "Quality Analysis", "Cost Analysis"])
    
    with tab1:
        st.subheader("Transfer Overview")
        col1, col2 = st.columns(2)
        
        with col1:
            st.write("**Quantity Distribution**")
            st.bar_chart(df.groupby("Destination")["Quantity (kg)"].sum())
        
        with col2:
            st.write("**Quality Grade Distribution**")
            quality_counts = df["Quality Grade"].value_counts()
            st.bar_chart(quality_counts)
    
    with tab2:
        st.subheader("Quality Analysis")
        quality_stats = df.groupby("Quality Grade").agg({
            "Quantity (kg)": "sum",
            "Cost ($)": "mean"
        }).round(2)
        st.dataframe(quality_stats, use_container_width=True)
    
    with tab3:
        st.subheader("Cost Analysis")
        cost_by_destination = df.groupby("Destination")["Cost ($)"].agg(["sum", "mean", "count"]).round(2)
        cost_by_destination.columns = ["Total Cost", "Avg Cost", "Count"]
        st.dataframe(cost_by_destination, use_container_width=True)


def main():
    """Main application entry point."""
    configure_page()
    
    # Check authentication
    if not login():
        return
    
    # Load data
    df = generate_sample_data()
    
    # Display sidebar and get selected page
    page = display_sidebar()
    
    # Display selected page
    if page == "Dashboard":
        dashboard_page(df)
    elif page == "Data View":
        data_view_page(df)
    elif page == "Analytics":
        analytics_page(df)


if __name__ == "__main__":
    main()
