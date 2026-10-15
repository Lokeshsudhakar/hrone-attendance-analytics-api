import json
import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_DETAILS = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGO_DB", "hrone_attendance_db")

client = MongoClient(MONGO_DETAILS)
db = client[DB_NAME]

def seed():
    # Clear existing collections if any
    db.employees.drop()
    db.attendance_logs.drop()

    # Seed employees
    if os.path.exists("sample_data/employees.json"):
        with open("sample_data/employees.json", "r") as f:
            employees = json.load(f)
            if employees:
                db.employees.insert_many(employees)
        print(f"Successfully seeded {db.employees.count_documents({})} employees.")

    # Seed attendance logs
    if os.path.exists("sample_data/attendance_logs.json"):
        with open("sample_data/attendance_logs.json", "r") as f:
            logs = json.load(f)
            if logs:
                db.attendance_logs.insert_many(logs)
        print(f"Successfully seeded {db.attendance_logs.count_documents({})} attendance logs.")

if __name__ == "__main__":
    seed()