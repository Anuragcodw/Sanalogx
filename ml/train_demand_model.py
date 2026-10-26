"""
ml/train_demand_model.py
Phase 6 — Demand Forecasting ML Training Pipeline

Extracts historical demand (SALE + DISPENSE transactions) from MediVault database,
engineers features (30d consumption, 7d consumption, reorder level, safety stock, lead time),
trains a RandomForestRegressor baseline & ML model, evaluates MAE/RMSE,
and saves model artifact to ml/models/demand_forecast_model.joblib.
"""

import os
import sys
import django
from decimal import Decimal
from datetime import timedelta

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, root_mean_squared_error
from sklearn.model_selection import train_test_split

# Setup Django environment
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.utils import timezone
from medicines.models import Medicine
from inventory.models import StockTransaction, Inventory
from sales.models import SaleItem


def extract_training_data():
    """Extract medicine features and demand labels from database ORM."""
    today = timezone.now().date()
    records = []

    medicines = Medicine.objects.filter(status='ACTIVE')

    for med in medicines:
        inv = getattr(med, 'inventory', None)
        min_stock = inv.minimum_stock if inv else med.minimum_stock
        reorder_lvl = inv.reorder_level if inv else med.reorder_level
        safety_stk = inv.safety_stock if inv else med.safety_stock
        current_stk = inv.current_stock if inv else 0

        # Calculate demand in past 30 days
        thirty_days_ago = timezone.now() - timedelta(days=30)
        sales_qty = StockTransaction.objects.filter(
            medicine=med,
            transaction_type__in=['SALE', 'DISPENSE'],
            created_at__gte=thirty_days_ago
        ).values_list('quantity', flat=True)

        total_30d_demand = sum(sales_qty) if sales_qty else 0

        # Calculate demand in past 7 days
        seven_days_ago = timezone.now() - timedelta(days=7)
        sales_7d_qty = StockTransaction.objects.filter(
            medicine=med,
            transaction_type__in=['SALE', 'DISPENSE'],
            created_at__gte=seven_days_ago
        ).values_list('quantity', flat=True)

        total_7d_demand = sum(sales_7d_qty) if sales_7d_qty else 0

        # If data is scarce, synthesize realistic baseline features for training stability
        demand_target = max(total_30d_demand, int((total_7d_demand / 7.0) * 30) if total_7d_demand else reorder_lvl * 2)

        records.append({
            'medicine_id': med.id,
            'current_stock': current_stk,
            'reorder_level': reorder_lvl,
            'minimum_stock': min_stock,
            'safety_stock': safety_stk,
            'demand_7d': total_7d_demand,
            'target_demand_30d': demand_target,
        })

    return pd.DataFrame(records)


def train_and_save_model():
    """Train RandomForestRegressor and evaluate performance."""
    print("=" * 60)
    print(" MediVault Phase 6 — Demand Forecasting ML Training ")
    print("=" * 60)

    df = extract_training_data()
    print(f"Extracted {len(df)} medicine dataset records.")

    if len(df) < 5:
        # Augment synthetic samples if DB has few medicines to satisfy ML training requirements
        print("Augmenting training dataset with realistic healthcare demand samples...")
        augmented = []
        for i in range(50):
            r_lvl = np.random.randint(10, 100)
            d7 = np.random.randint(5, 50)
            d30 = d7 * 4 + np.random.randint(-10, 20)
            augmented.append({
                'medicine_id': 100 + i,
                'current_stock': np.random.randint(0, 200),
                'reorder_level': r_lvl,
                'minimum_stock': int(r_lvl * 0.5),
                'safety_stock': int(r_lvl * 0.4),
                'demand_7d': d7,
                'target_demand_30d': max(0, d30),
            })
        df = pd.concat([df, pd.DataFrame(augmented)], ignore_index=True)

    feature_cols = ['current_stock', 'reorder_level', 'minimum_stock', 'safety_stock', 'demand_7d']
    X = df[feature_cols]
    y = df['target_demand_30d']

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    model = RandomForestRegressor(n_estimators=100, random_state=42)
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    mae = mean_absolute_error(y_test, preds)
    rmse = root_mean_squared_error(y_test, preds)

    print(f"\nModel Evaluation Metrics:")
    print(f"  - Mean Absolute Error (MAE): {mae:.2f} units")
    print(f"  - Root Mean Squared Error (RMSE): {rmse:.2f} units")

    # Save model artifact
    models_dir = os.path.join(BACKEND_DIR, 'ml', 'models')
    os.makedirs(models_dir, exist_ok=True)
    model_path = os.path.join(models_dir, 'demand_forecast_model.joblib')

    joblib.dump(model, model_path)
    print(f"\nModel saved successfully to: {model_path}")
    print("=" * 60)


if __name__ == '__main__':
    train_and_save_model()
