"""Main Streamlit application for JuteTransfer."""

import streamlit as st
import pandas as pd
from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode, DataReturnMode
from src.jutetransfer.auth import login, logout, is_authenticated, get_username
from src.jutetransfer.data import generate_sample_data, get_summary_statistics
from src.jutetransfer.database import DatabaseConnection
from src.jutetransfer.schemas import load_schemas, get_all_table_names, refresh_schemas_from_db


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
                ["Dashboard", "Data View", "Analytics",
                    "Jute MR Table", "Schema Viewer"],
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

    tab1, tab2, tab3 = st.tabs(
        ["Overview", "Quality Analysis", "Cost Analysis"])

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
        cost_by_destination = df.groupby("Destination")["Cost ($)"].agg([
            "sum", "mean", "count"]).round(2)
        cost_by_destination.columns = ["Total Cost", "Avg Cost", "Count"]
        st.dataframe(cost_by_destination, use_container_width=True)


def jute_mr_table_page():
    """Display Jute MR table from database with year/month filter."""
    st.title("📦 Jute MR Table")

    try:
        # Year and Month filter inputs
        col1, col2 = st.columns(2)

        from datetime import datetime
        current_year = datetime.now().year
        current_month = datetime.now().month

        with col1:
            selected_year = st.selectbox(
                "Select Year",
                options=list(range(current_year, current_year - 10, -1)),
                index=0
            )

        with col2:
            month_names = [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"
            ]
            selected_month = st.selectbox(
                "Select Month",
                options=list(range(1, 13)),
                format_func=lambda x: month_names[x - 1],
                index=current_month - 1
            )

        # Query with date filter
        query = """
            SELECT * FROM jute_mr 
            WHERE YEAR(jute_gate_entry_date) = :year 
            AND MONTH(jute_gate_entry_date) = :month
        """
        df = DatabaseConnection.execute_query(
            query, {"year": selected_year, "month": selected_month})

        if df is not None and not df.empty:
            st.success(
                f"✅ Loaded {len(df)} records for {month_names[selected_month - 1]} {selected_year}")

            # Display record count
            st.metric("Total Records", len(df))

            st.markdown("---")

            # Display the data in AgGrid
            st.subheader("📊 Jute MR Data")

            # Use st.dataframe as a reliable fallback with AgGrid styling
            st.dataframe(
                df,
                height=600,
                use_container_width=True,
            )
        else:
            st.warning(
                f"⚠️ No data found for {month_names[selected_month - 1]} {selected_year}")

    except Exception as e:
        st.error(f"❌ Error loading jute_mr table: {str(e)}")
        st.info("💡 Make sure the database is configured and the jute_mr table exists")


def schema_viewer_page():
    """Display database table schemas."""
    st.title("🗂️ Database Schema Viewer")

    # Refresh button
    col1, col2 = st.columns([1, 4])
    with col1:
        if st.button("🔄 Refresh Schemas", use_container_width=True):
            with st.spinner("Fetching schemas from database..."):
                refresh_schemas_from_db()
                st.success("✅ Schemas refreshed!")
                st.rerun()

    # Load schemas
    schemas = load_schemas()

    if not schemas:
        st.warning(
            "⚠️ No schemas found. Click 'Refresh Schemas' to fetch from database.")
        return

    # Display available tables
    available_tables = [name for name,
                        schema in schemas.items() if schema is not None]
    unavailable_tables = [name for name,
                          schema in schemas.items() if schema is None]

    st.info(
        f"📊 **{len(available_tables)}** tables available | **{len(unavailable_tables)}** tables not found")

    # Table selector
    selected_table = st.selectbox(
        "Select Table",
        options=available_tables,
        index=0 if available_tables else None
    )

    if selected_table and schemas.get(selected_table):
        st.markdown("---")
        st.subheader(f"📋 Schema: `{selected_table}`")

        # Convert schema to DataFrame for display
        schema_df = pd.DataFrame(schemas[selected_table])

        # Display column count
        st.metric("Total Columns", len(schema_df))

        # Display schema table
        st.dataframe(
            schema_df,
            use_container_width=True,
            height=400
        )

        # Show column names as a list
        with st.expander("📝 Column Names (copy-friendly)"):
            column_names = schema_df['Field'].tolist()
            st.code(", ".join(column_names))
            st.code("\n".join(column_names))

    # Show unavailable tables
    if unavailable_tables:
        with st.expander("❌ Unavailable Tables"):
            for table in unavailable_tables:
                st.write(f"- {table}")


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
    elif page == "Jute MR Table":
        jute_mr_table_page()
    elif page == "Schema Viewer":
        schema_viewer_page()


if __name__ == "__main__":
    main()
