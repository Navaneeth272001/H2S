import flwr as fl
import lightgbm as lgb
import pandas as pd
from sqlalchemy import create_engine
import numpy as np
from src.config import DATABASE_URL

# Helper to load data synchronously from Postgres
def load_local_data():
    # Swap asyncpg for psycopg2 to use pandas read_sql easily
    sync_db_url = DATABASE_URL.replace("postgresql+asyncpg", "postgresql")
    engine = create_engine(sync_db_url)
    
    # In a real scenario, we would load energy_hourly and weather_hourly, join them and engineer features
    query = """
    SELECT 
        e.start_time,
        SUM(e.total_energy_wh) as total_consumption,
        w.temperature,
        w.humidity,
        w.wind_speed,
        w.ghi
    FROM energy_hourly e
    JOIN weather_hourly w ON e.start_time = w.timestamp
    GROUP BY e.start_time, w.temperature, w.humidity, w.wind_speed, w.ghi
    ORDER BY e.start_time
    """
    try:
        df = pd.read_sql(query, engine)
        
        # Simple feature engineering (mock for FL structure)
        df['hour'] = df['start_time'].dt.hour
        
        # Target: Total Consumption
        X = df[['temperature', 'humidity', 'wind_speed', 'ghi', 'hour']].values
        y = df['total_consumption'].values
        return X, y
    except Exception as e:
        print(f"Error loading DB: {e}")
        return np.zeros((10, 5)), np.zeros(10)

class LightGBMClient(fl.client.NumPyClient):
    def __init__(self, X, y):
        self.X = X
        self.y = y
        self.model = lgb.LGBMRegressor(n_estimators=100, learning_rate=0.1)
        self.model.fit(self.X, self.y) # Initial fit

    def get_parameters(self, config):
        # LightGBM Trees cannot be directly serialized as weights like NNs.
        # For tree-based federated learning via histogram aggregation or tree sending,
        # we can extract booster string or use specific FL tree libraries.
        # As a placeholder for FL integration (gradients/histograms):
        print("[Client] get_parameters called.")
        # Returning dummy parameter list for Flower compatibility
        return [np.array([1.0])]

    def fit(self, parameters, config):
        print("[Client] fit called. Updating model with local data.")
        self.model.fit(self.X, self.y)
        # Would return updated histograms/gradients here
        return self.get_parameters(config), len(self.X), {}

    def evaluate(self, parameters, config):
        print("[Client] evaluate called.")
        preds = self.model.predict(self.X)
        loss = float(np.mean((self.y - preds) ** 2)) # MSE
        return loss, len(self.X), {"rmse": np.sqrt(loss)}

def main():
    print("Loading local data from PostgreSQL...")
    X, y = load_local_data()
    print(f"Loaded {len(X)} records.")

    print("Starting Flower Client for LightGBM Federated Learning...")
    # Cloud FL server URL (Placeholder)
    fl.client.start_numpy_client(server_address="localhost:8080", client=LightGBMClient(X, y))

if __name__ == "__main__":
    main()
