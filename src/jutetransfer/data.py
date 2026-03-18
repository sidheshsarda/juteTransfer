"""Sample data generator for JuteTransfer."""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def generate_sample_data() -> pd.DataFrame:
    """
    Generate sample data for demonstration purposes.
    
    Returns:
        A pandas DataFrame with sample jute transfer data
    """
    np.random.seed(42)
    
    # Generate sample data
    num_records = 50
    
    data = {
        "Transfer ID": [f"JT{str(i).zfill(5)}" for i in range(1, num_records + 1)],
        "Date": [
            (datetime.now() - timedelta(days=np.random.randint(0, 365))).strftime("%Y-%m-%d")
            for _ in range(num_records)
        ],
        "Source Location": np.random.choice(
            ["Warehouse A", "Warehouse B", "Warehouse C", "Warehouse D"], num_records
        ),
        "Destination": np.random.choice(
            ["Factory 1", "Factory 2", "Factory 3", "Port A", "Port B"], num_records
        ),
        "Quantity (kg)": np.random.randint(100, 5000, num_records),
        "Quality Grade": np.random.choice(["A", "B", "C", "Premium"], num_records),
        "Status": np.random.choice(
            ["Completed", "In Transit", "Pending", "Delayed"], 
            num_records,
            p=[0.6, 0.2, 0.1, 0.1]
        ),
        "Cost ($)": np.random.uniform(500, 10000, num_records).round(2),
    }
    
    df = pd.DataFrame(data)
    return df


def get_summary_statistics(df: pd.DataFrame) -> dict:
    """
    Calculate summary statistics for the data.
    
    Args:
        df: DataFrame containing jute transfer data
    
    Returns:
        Dictionary with summary statistics
    """
    return {
        "Total Transfers": len(df),
        "Total Quantity (kg)": df["Quantity (kg)"].sum(),
        "Total Cost ($)": round(df["Cost ($)"].sum(), 2),
        "Average Quantity (kg)": round(df["Quantity (kg)"].mean(), 2),
        "Completed Transfers": len(df[df["Status"] == "Completed"]),
        "In Transit": len(df[df["Status"] == "In Transit"]),
    }
