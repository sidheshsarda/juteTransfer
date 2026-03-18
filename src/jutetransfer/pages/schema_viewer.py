"""Schema Viewer page for JuteTransfer application."""

import streamlit as st
import pandas as pd

from ..schemas import load_schemas, refresh_schemas_from_db


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
