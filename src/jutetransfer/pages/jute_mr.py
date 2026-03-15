"""Jute MR Table page for JuteTransfer application."""

import streamlit as st
import pandas as pd
from datetime import datetime

from ..queries import (
    get_companies,
    get_branches_by_company,
    get_company_branch_options,
    get_jute_mr_with_line_items,
    get_next_mr_number
)

# Number of transfer column sets
NUM_TRANSFERS = 8


def build_transfer_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add transfer columns to the dataframe.
    
    Args:
        df: Source dataframe
        
    Returns:
        DataFrame with transfer columns added
    """
    # Get the position after Status column
    status_idx = df.columns.get_loc('Status') + 1
    
    # Store original columns for reference (as lists to avoid index issues)
    original_mr_rate = df['MR Rate'].fillna(0).tolist()
    original_mr_date = df['MR DATE'].tolist()
    original_claim_amount = df['Claim Amount'].fillna(0).tolist()
    original_weight = df['Weight (KG)'].fillna(0).tolist()
    
    # Add transfer columns for each transfer set
    col_position = status_idx
    
    for n in range(1, NUM_TRANSFERS + 1):
        suffix = f"_{n}"
        
        # co_n - company dropdown
        df.insert(col_position, f'co{suffix}', "")
        col_position += 1
        
        # mr_no_n - auto-generated (will be calculated based on selection)
        df.insert(col_position, f'mr_no{suffix}', None)
        col_position += 1
        
        # mr_date_n - for n=1 use original, others manual entry
        if n == 1:
            df.insert(col_position, f'mr_date{suffix}', original_mr_date)
        else:
            df.insert(col_position, f'mr_date{suffix}', None)
        col_position += 1
        
        # %rate_increase_n - only for n >= 2
        if n >= 2:
            df.insert(col_position, f'%rate_increase{suffix}', 0.0)
            col_position += 1
        
        # mr_rate_n - for n=1 use original, others calculated
        if n == 1:
            df.insert(col_position, f'mr_rate{suffix}', original_mr_rate)
        else:
            df.insert(col_position, f'mr_rate{suffix}', 0.0)
        col_position += 1
        
        # total_amount_n - calculated: weight * mr_rate / 100
        if n == 1:
            total_amounts = [w * r / 100 for w, r in zip(original_weight, original_mr_rate)]
            df.insert(col_position, f'total_amount{suffix}', total_amounts)
        else:
            df.insert(col_position, f'total_amount{suffix}', 0.0)
        col_position += 1
        
        # claim_amount_n - for n=1 use original, others cascade
        if n == 1:
            df.insert(col_position, f'claim_amount{suffix}', original_claim_amount)
        else:
            df.insert(col_position, f'claim_amount{suffix}', 0.0)
        col_position += 1
        
        # net_amount_n - calculated: total_amount - claim_amount
        if n == 1:
            net_amounts = [(w * r / 100) - c for w, r, c in zip(original_weight, original_mr_rate, original_claim_amount)]
            df.insert(col_position, f'net_amount{suffix}', net_amounts)
        else:
            df.insert(col_position, f'net_amount{suffix}', 0.0)
        col_position += 1
    
    return df


def build_column_config(co_branch_options: list, source_co_branch: str) -> dict:
    """Build column configuration for st.data_editor.
    
    Args:
        co_branch_options: List of company-branch dropdown options
        source_co_branch: The source company-branch label to potentially exclude
        
    Returns:
        dict: Column configuration for data_editor
    """
    config = {}
    
    for n in range(1, NUM_TRANSFERS + 1):
        suffix = f"_{n}"
        
        # Filter out source company if this is the last transfer (when it matches source)
        # For now, show all options
        options = co_branch_options
        
        config[f'co{suffix}'] = st.column_config.SelectboxColumn(
            f"Co_{n}",
            help=f"Select target company-branch for transfer {n}",
            options=options,
            required=False,
        )
        
        config[f'mr_no{suffix}'] = st.column_config.NumberColumn(
            f"MR No_{n}",
            help=f"Auto-generated MR number for transfer {n}",
            disabled=True,
        )
        
        config[f'mr_date{suffix}'] = st.column_config.DateColumn(
            f"MR Date_{n}",
            help=f"MR date for transfer {n}",
            disabled=(n == 1),  # First one uses original date
        )
        
        if n >= 2:
            config[f'%rate_increase{suffix}'] = st.column_config.NumberColumn(
                f"% Rate Increase_{n}",
                help=f"Percentage rate increase for transfer {n}",
                min_value=-100.0,
                max_value=1000.0,
                step=0.1,
                format="%.2f%%",
            )
        
        config[f'mr_rate{suffix}'] = st.column_config.NumberColumn(
            f"MR Rate_{n}",
            help=f"MR rate for transfer {n}",
            disabled=True,
            format="%.2f",
        )
        
        config[f'total_amount{suffix}'] = st.column_config.NumberColumn(
            f"Total Amount_{n}",
            help=f"Total amount for transfer {n}",
            disabled=True,
            format="%.2f",
        )
        
        config[f'claim_amount{suffix}'] = st.column_config.NumberColumn(
            f"Claim Amount_{n}",
            help=f"Claim amount for transfer {n}",
            disabled=True,
            format="%.2f",
        )
        
        config[f'net_amount{suffix}'] = st.column_config.NumberColumn(
            f"Net Amount_{n}",
            help=f"Net amount for transfer {n}",
            disabled=True,
            format="%.2f",
        )
    
    return config


def get_disabled_columns(df: pd.DataFrame) -> list:
    """Get list of columns that should be disabled (not editable).
    
    Returns:
        list: Column names that should be disabled
    """
    # Source data columns are disabled
    source_columns = [
        'Jute Gate Entry No', 'Jute Gate Entry Date', 'PO.No.', 'PO DATE',
        'EJM MR No.', 'CO_MR_No', 'MR DATE', 'Party Name', 'Item Quality',
        'Weight (KG)', 'Invoice No', 'Invoice Date', 'Status',
        'MR Rate', 'Total Amount', 'Claim Rate', 'Claim Amount', 'Net Total'
    ]
    
    disabled = list(source_columns)
    
    # Add calculated/auto columns for transfers
    for n in range(1, NUM_TRANSFERS + 1):
        suffix = f"_{n}"
        disabled.extend([
            f'mr_no{suffix}',
            f'mr_rate{suffix}',
            f'total_amount{suffix}',
            f'claim_amount{suffix}',
            f'net_amount{suffix}',
        ])
        # First transfer date is also disabled (uses original)
        if n == 1:
            disabled.append(f'mr_date{suffix}')
    
    return [col for col in disabled if col in df.columns]


def validate_and_clean_transfers(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """Validate transfer selections and clear invalid ones.
    
    Ensures co_n cannot be selected until co_(n-1) is filled.
    
    Args:
        df: DataFrame with transfer data
        
    Returns:
        Tuple of (cleaned DataFrame, list of validation warnings)
    """
    result_df = df.copy()
    warnings = []
    
    for idx in range(len(result_df)):
        row_id = result_df.loc[idx, 'Jute Gate Entry No']
        
        for n in range(2, NUM_TRANSFERS + 1):
            curr_suffix = f"_{n}"
            prev_suffix = f"_{n-1}"
            
            curr_co = result_df.loc[idx, f'co{curr_suffix}']
            prev_co = result_df.loc[idx, f'co{prev_suffix}']
            
            # Check if current is selected but previous is not
            curr_selected = curr_co and curr_co != "" and pd.notna(curr_co)
            prev_selected = prev_co and prev_co != "" and pd.notna(prev_co)
            
            if curr_selected and not prev_selected:
                # Clear the invalid selection
                result_df.loc[idx, f'co{curr_suffix}'] = ""
                result_df.loc[idx, f'mr_no{curr_suffix}'] = None
                result_df.loc[idx, f'mr_date{curr_suffix}'] = None
                result_df.loc[idx, f'%rate_increase{curr_suffix}'] = 0.0
                result_df.loc[idx, f'mr_rate{curr_suffix}'] = 0.0
                result_df.loc[idx, f'total_amount{curr_suffix}'] = 0.0
                result_df.loc[idx, f'claim_amount{curr_suffix}'] = 0.0
                result_df.loc[idx, f'net_amount{curr_suffix}'] = 0.0
                warnings.append(f"Row {row_id}: co_{n} cleared - co_{n-1} must be selected first")
    
    return result_df, warnings


def recalculate_transfers(df: pd.DataFrame, co_branch_mapping: dict,
                          source_co_branch: str) -> pd.DataFrame:
    """Recalculate transfer values based on user inputs.
    
    Args:
        df: DataFrame with user edits
        co_branch_mapping: Dict mapping label to (co_id, branch_id)
        source_co_branch: The source company-branch label
        
    Returns:
        DataFrame with recalculated values
    """
    result_df = df.copy()
    
    for idx in range(len(result_df)):
        weight = result_df.loc[idx, 'Weight (KG)'] or 0
        
        # Track previous values for cascading - start from original source values
        prev_mr_rate = result_df.loc[idx, 'MR Rate'] or 0
        prev_claim_amount = result_df.loc[idx, 'Claim Amount'] or 0
        
        for n in range(1, NUM_TRANSFERS + 1):
            suffix = f"_{n}"
            co_col = f'co{suffix}'
            
            # Get selected company
            selected_co = result_df.loc[idx, co_col]
            
            # If company is selected
            if selected_co and selected_co != "" and pd.notna(selected_co):
                # Check if this is the source company (should stop cascading after this)
                is_source = (selected_co == source_co_branch)
                
                # Get MR number
                if selected_co in co_branch_mapping:
                    co_id, branch_id = co_branch_mapping[selected_co]
                    mr_no = get_next_mr_number(co_id, branch_id)
                    result_df.loc[idx, f'mr_no{suffix}'] = mr_no
                
                # Calculate rate
                if n == 1:
                    # First transfer uses original rate
                    mr_rate = prev_mr_rate
                else:
                    # Get % increase and calculate new rate from previous transfer's rate
                    pct_increase = result_df.loc[idx, f'%rate_increase{suffix}'] or 0
                    mr_rate = prev_mr_rate * (1 + float(pct_increase) / 100)
                
                result_df.loc[idx, f'mr_rate{suffix}'] = mr_rate
                
                # Calculate total amount
                total_amount = weight * mr_rate / 100
                result_df.loc[idx, f'total_amount{suffix}'] = total_amount
                
                # Claim amount cascades from previous
                result_df.loc[idx, f'claim_amount{suffix}'] = prev_claim_amount
                
                # Net amount
                net_amount = total_amount - prev_claim_amount
                result_df.loc[idx, f'net_amount{suffix}'] = net_amount
                
                # Update prev values for next iteration
                prev_mr_rate = mr_rate
                # Claim amount stays same for cascading
                
                # If source is selected, stop calculating more transfers
                if is_source:
                    break
            else:
                # No selection, values remain as initialized (0 for n>=2)
                pass
    
    return result_df


def log_transfer_data(df: pd.DataFrame):
    """Log the transfer data to console for debugging.
    
    Args:
        df: DataFrame with transfer data
    """
    transfer_cols = []
    for n in range(1, NUM_TRANSFERS + 1):
        suffix = f"_{n}"
        cols = [f'co{suffix}', f'mr_no{suffix}', f'mr_date{suffix}']
        if n >= 2:
            cols.append(f'%rate_increase{suffix}')
        cols.extend([f'mr_rate{suffix}', f'total_amount{suffix}', 
                    f'claim_amount{suffix}', f'net_amount{suffix}'])
        transfer_cols.extend([c for c in cols if c in df.columns])
    
    if transfer_cols:
        transfer_data = df[['Jute Gate Entry No'] + transfer_cols]
        # Filter rows that have at least one co_n selected
        has_selection = False
        for n in range(1, NUM_TRANSFERS + 1):
            col = f'co_{n}'
            if col in df.columns:
                has_selection = has_selection | (df[col].notna() & (df[col] != "")).any()
        
        print("\n=== Transfer Data ===")
        print(transfer_data.to_string())
        print("=====================\n")


def jute_mr_table_page():
    """Display Jute MR table from database with company, branch, year/month filter."""
    st.title("📦 Jute MR Table")

    try:
        current_year = datetime.now().year
        current_month = datetime.now().month

        # Fetch companies for dropdown
        company_options = get_companies()

        # Company and Branch filter inputs (first row)
        col1, col2 = st.columns(2)

        with col1:
            selected_company_name = st.selectbox(
                "Select Company",
                options=list(company_options.keys()),
                index=0 if company_options else None
            )
            selected_company_id = company_options.get(selected_company_name) if selected_company_name else None

        # Fetch branches based on selected company
        if selected_company_id:
            branch_options = get_branches_by_company(selected_company_id)
        else:
            branch_options = {}

        with col2:
            selected_branch_name = st.selectbox(
                "Select Branch",
                options=list(branch_options.keys()),
                index=0 if branch_options else None
            )
            selected_branch_id = branch_options.get(selected_branch_name) if selected_branch_name else None

        # Year and Month filter inputs (second row)
        col3, col4 = st.columns(2)

        with col3:
            selected_year = st.selectbox(
                "Select Year",
                options=list(range(current_year, current_year - 10, -1)),
                index=0
            )

        with col4:
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

        # Fetch company-branch options for co_n dropdowns (independent query)
        co_branch_options, co_branch_mapping = get_company_branch_options()
        
        # Determine source company-branch label for comparison
        source_co_branch = None
        if selected_company_name and selected_branch_name:
            # Find matching label in options
            for label in co_branch_options:
                if label and selected_branch_name in label:
                    source_co_branch = label
                    break

        # Create a unique key for session state based on filters
        filter_key = f"{selected_company_id}_{selected_branch_id}_{selected_year}_{selected_month}"
        
        # Fetch MR data with line items
        df = get_jute_mr_with_line_items(
            year=selected_year,
            month=selected_month,
            company_id=selected_company_id,
            branch_id=selected_branch_id
        )

        if df is not None and not df.empty:
            filter_info = f"{month_names[selected_month - 1]} {selected_year}"
            if selected_company_name:
                filter_info = f"{selected_company_name} - {filter_info}"
            if selected_branch_name:
                filter_info = f"{selected_branch_name} @ {filter_info}"
            st.success(
                f"✅ Loaded {len(df)} records for {filter_info}")

            # Display record count
            st.metric("Total Records", len(df))

            st.markdown("---")

            # Check if we need to initialize or reset the dataframe in session state
            if f'mr_data_{filter_key}' not in st.session_state:
                # Build transfer columns for fresh data
                df = build_transfer_columns(df)
                st.session_state[f'mr_data_{filter_key}'] = df
            
            # Get the dataframe from session state
            working_df = st.session_state[f'mr_data_{filter_key}'].copy()
            
            # Build column configuration
            column_config = build_column_config(co_branch_options, source_co_branch)
            
            # Get disabled columns
            disabled_cols = get_disabled_columns(working_df)

            # Display the data
            st.subheader("📊 Jute MR Data")

            # Use st.data_editor to allow editing
            edited_df = st.data_editor(
                working_df,
                height=600,
                width="stretch",
                column_config=column_config,
                disabled=disabled_cols,
                num_rows="fixed",
                key=f"data_editor_{filter_key}"
            )
            
            # Check for changes and auto-recalculate
            has_changes = not edited_df.equals(working_df)
            
            if has_changes:
                # Validate and clean invalid selections
                validated_df, validation_warnings = validate_and_clean_transfers(edited_df)
                
                # Show warnings if any
                for warning in validation_warnings:
                    st.warning(f"⚠️ {warning}")
                
                # Recalculate transfer values
                recalculated_df = recalculate_transfers(validated_df, co_branch_mapping, source_co_branch)
                
                # Update session state
                st.session_state[f'mr_data_{filter_key}'] = recalculated_df
                
                # Log for debugging
                log_transfer_data(recalculated_df)
                
                # Rerun to show updated values
                st.rerun()
            
            # Reset button
            if st.button("🔃 Reset Data", use_container_width=True):
                # Clear session state for this filter
                if f'mr_data_{filter_key}' in st.session_state:
                    del st.session_state[f'mr_data_{filter_key}']
                st.rerun()
            
            # Log data for debugging
            log_transfer_data(edited_df)
            
        else:
            filter_info = f"{month_names[selected_month - 1]} {selected_year}"
            if selected_company_name:
                filter_info = f"{selected_company_name} - {filter_info}"
            if selected_branch_name:
                filter_info = f"{selected_branch_name} @ {filter_info}"
            st.warning(
                f"⚠️ No data found for {filter_info}")

    except Exception as e:
        st.error(f"❌ Error loading jute_mr table: {str(e)}")
        st.info("💡 Make sure the database is configured and the jute_mr table exists")
