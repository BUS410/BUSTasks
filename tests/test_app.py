import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import PAGE_SIZE, app as main_app, create_app
from flask_app import app as pythonanywhere_app


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "test.sqlite3"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": str(database),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pythonanywhere_entry_point_uses_main_app(self):
        self.assertIs(pythonanywhere_app, main_app)

    def csrf_token(self):
        response = self.client.get("/tasks/new")
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def create_task(self, title="Тестовая задача", **overrides):
        values = {
            "csrf_token": self.csrf_token(),
            "title": title,
            "description": "Описание",
            "due_date": "2026-11-01",
            "priority": "high",
            "status": "new",
            "assignee": "Алексей",
        }
        values.update(overrides)
        return self.client.post("/tasks/new", data=values, follow_redirects=True)

    def test_create_task_and_add_comment(self):
        response = self.create_task()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Тестовая задача".encode(), response.data)
        self.assertIn("Алексей".encode(), response.data)

        token = self.csrf_token()
        response = self.client.post(
            "/tasks/1/comments",
            data={"csrf_token": token, "author": "Мария", "body": "Начала выполнять"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Начала выполнять".encode(), response.data)
        self.assertIn("Мария".encode(), response.data)

    def test_new_task_form_defaults_to_today_and_redirects_to_home(self):
        response = self.client.get("/tasks/new")
        self.assertIn(
            f'value="{date.today().isoformat()}"'.encode(), response.data
        )
        self.assertIn(
            'name="assignee" type="text" maxlength="100"'.encode(),
            response.data,
        )
        self.assertIn('value="Все"'.encode(), response.data)
        token = re.search(
            rb'name="csrf_token" value="([^"]+)"', response.data
        ).group(1).decode()

        response = self.client.post(
            "/tasks/new",
            data={
                "csrf_token": token,
                "title": "Новая задача",
                "description": "",
                "due_date": date.today().isoformat(),
                "priority": "medium",
                "status": "new",
                "assignee": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_homepage_filters_and_sorts_tasks(self):
        self.create_task(title="Задача Я", priority="high", status="new")
        self.create_task(title="Задача А", priority="high", status="new")
        self.create_task(title="Задача Б", priority="high", status="completed")
        self.create_task(title="Задача В", priority="low", status="new")

        response = self.client.get(
            "/?status=new&priority=high&sort=title_asc"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'class="task-card'), 2)
        self.assertLess(
            response.data.index("Задача А".encode()),
            response.data.index("Задача Я".encode()),
        )
        self.assertNotIn("Задача Б".encode(), response.data)
        self.assertNotIn("Задача В".encode(), response.data)

    def test_task_can_be_deleted_from_homepage(self):
        self.create_task(title="Задача для удаления")

        response = self.client.post(
            "/tasks/1/delete",
            data={
                "csrf_token": self.csrf_token(),
                "filter_status": "new",
                "filter_priority": "high",
                "sort": "title_asc",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Задача удалена.".encode(), response.data)
        self.assertNotIn("Задача для удаления".encode(), response.data)
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            connection.close()
        self.assertEqual(count, 0)

    def test_completed_task_is_marked_inactive(self):
        self.create_task(status="completed")
        response = self.client.get("/")
        self.assertIn(b"task-card-completed", response.data)
        self.assertIn("Завершена".encode(), response.data)

    def test_dates_are_displayed_in_readable_format(self):
        self.create_task()
        response = self.client.get("/tasks/1")
        self.assertIn(b"01.11.2026", response.data)
        self.assertRegex(response.data, rb"\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}")

    def test_long_task_text_is_rendered_in_list(self):
        long_title = "ОченьДлинноеНазваниеЗадачиБезПробелов" * 8
        long_assignee = "ИсполнительБезПробелов" * 8
        response = self.create_task(title=long_title, assignee=long_assignee)

        self.assertEqual(response.status_code, 200)
        self.assertIn(long_title.encode(), response.data)
        self.assertIn(long_assignee.encode(), response.data)

    def test_status_can_be_changed_from_task_list(self):
        self.create_task()
        response = self.client.post(
            "/tasks/1/status",
            data={
                "csrf_token": self.csrf_token(),
                "status": "completed",
                "page": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/?page=1")

        response = self.client.get("/")
        self.assertIn(b"task-card-completed", response.data)
        self.assertIn(b'<option value="completed" selected>', response.data)

    def test_invalid_list_status_does_not_change_task(self):
        self.create_task()
        self.client.post(
            "/tasks/1/status",
            data={
                "csrf_token": self.csrf_token(),
                "status": "invalid",
                "page": "1",
            },
        )

        response = self.client.get("/tasks/1")
        self.assertIn("Новая".encode(), response.data)
        self.assertIn("Выберите корректный статус.".encode(), response.data)

    def test_list_is_paginated_by_twenty(self):
        with self.app.app_context():
            from flask import current_app
            import sqlite3

            connection = sqlite3.connect(current_app.config["DATABASE"])
            connection.executemany(
                "INSERT INTO tasks (title, priority, status) VALUES (?, 'medium', 'new')",
                [(f"Задача {number}",) for number in range(PAGE_SIZE + 1)],
            )
            connection.commit()
            connection.close()

        first_page = self.client.get("/")
        self.assertEqual(first_page.data.count(b'class="task-card'), PAGE_SIZE)
        self.assertIn("Страница 1 из 2".encode(), first_page.data)
        second_page = self.client.get("/?page=2")
        self.assertEqual(second_page.data.count(b'class="task-card'), 1)

    def test_invalid_form_is_rejected_without_creating_task(self):
        response = self.client.post(
            "/tasks/new",
            data={
                "csrf_token": self.csrf_token(),
                "title": "",
                "priority": "high",
                "status": "new",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Укажите название задачи.".encode(), response.data)
        self.assertEqual(self.client.get("/").data.count(b'class="task-card'), 0)

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/tasks/new", data={"title": "Запрос без токена"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
