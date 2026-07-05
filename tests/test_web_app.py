"""Tests for web.app — Flask web dashboard."""

import os
import sys
import pytest

# Ensure project root is on path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from web.app import app, _db


@pytest.fixture
def client():
    """Create a Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _auth_headers():
    """Return Basic Auth headers matching config defaults (admin/admin)."""
    import base64
    creds = base64.b64encode(b"admin:admin").decode("utf-8")
    return {"Authorization": f"Basic {creds}"}


class TestDashboard:
    def test_dashboard_requires_auth(self, client):
        response = client.get("/")
        assert response.status_code == 401

    def test_dashboard_with_auth(self, client):
        response = client.get("/", headers=_auth_headers())
        assert response.status_code == 200
        assert b"Dashboard" in response.data


class TestEventsPage:
    def test_events_requires_auth(self, client):
        response = client.get("/events")
        assert response.status_code == 401

    def test_events_with_auth(self, client):
        response = client.get("/events", headers=_auth_headers())
        assert response.status_code == 200
        assert b"Event Log" in response.data

    def test_events_pagination(self, client):
        response = client.get("/events?page=1", headers=_auth_headers())
        assert response.status_code == 200


class TestVehiclesPage:
    def test_vehicles_requires_auth(self, client):
        response = client.get("/vehicles")
        assert response.status_code == 401

    def test_vehicles_with_auth(self, client):
        response = client.get("/vehicles", headers=_auth_headers())
        assert response.status_code == 200
        assert b"Vehicle Whitelist" in response.data


class TestVehicleCRUD:
    def test_add_vehicle(self, client):
        response = client.post(
            "/vehicles/add",
            data={"plate_text": "TESTPLATE1", "owner_name": "Tester", "access_level": "resident"},
            headers=_auth_headers(),
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"TESTPLATE1" in response.data

    def test_add_vehicle_empty_plate(self, client):
        response = client.post(
            "/vehicles/add",
            data={"plate_text": "", "owner_name": ""},
            headers=_auth_headers(),
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"required" in response.data

    def test_remove_vehicle(self, client):
        # Add then remove
        client.post(
            "/vehicles/add",
            data={"plate_text": "REMOVEME"},
            headers=_auth_headers(),
        )
        response = client.post(
            "/vehicles/remove",
            data={"plate_text": "REMOVEME"},
            headers=_auth_headers(),
            follow_redirects=True,
        )
        assert response.status_code == 200


class TestAPIEndpoints:
    def test_api_status(self, client):
        response = client.get("/api/status", headers=_auth_headers())
        assert response.status_code == 200
        data = response.get_json()
        assert "status" in data
        assert data["status"] == "running"
        assert "total_events" in data

    def test_api_events(self, client):
        response = client.get("/api/events", headers=_auth_headers())
        assert response.status_code == 200
        data = response.get_json()
        assert isinstance(data, list)

    def test_api_status_no_auth(self, client):
        response = client.get("/api/status")
        assert response.status_code == 401
