import os
import secrets
import sqlite3
from datetime import date, datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


STATUSES = {
    "new": "Новая",
    "in_progress": "В работе",
    "completed": "Завершена",
}
PRIORITIES = {
    "low": "Низкий",
    "medium": "Средний",
    "high": "Высокий",
}
PAGE_SIZE = 20
SORT_OPTIONS = {
    "created_desc": "created_at DESC, id DESC",
    "created_asc": "created_at ASC, id ASC",
    "due_asc": "due_date IS NULL, due_date ASC, id DESC",
    "due_desc": "due_date IS NULL, due_date DESC, id DESC",
    "title_asc": "title COLLATE NOCASE ASC, id ASC",
    "title_desc": "title COLLATE NOCASE DESC, id DESC",
}


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
        DATABASE=os.path.join(app.instance_path, "tasks.sqlite3"),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(app.config["DATABASE"])
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                due_date TEXT,
                priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high')),
                status TEXT NOT NULL CHECK (status IN ('new', 'in_progress', 'completed')),
                assignee TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                author TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    finally:
        connection.close()

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            submitted_token = request.form.get("csrf_token", "")
            if not submitted_token or not secrets.compare_digest(
                submitted_token, session.get("csrf_token", "")
            ):
                abort(400, description="Недействительный токен формы. Обновите страницу.")

    @app.context_processor
    def inject_template_helpers():
        def csrf_token():
            if "csrf_token" not in session:
                session["csrf_token"] = secrets.token_urlsafe(32)
            return session["csrf_token"]

        return {
            "csrf_token": csrf_token,
            "format_date": format_date,
            "STATUSES": STATUSES,
            "PRIORITIES": PRIORITIES,
        }

    def format_date(value):
        if not value:
            return ""
        if " " in value or "T" in value:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y, %H:%M")
        return date.fromisoformat(value).strftime("%d.%m.%Y")

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_error=None):
        database = g.pop("db", None)
        if database is not None:
            database.close()

    def get_task(task_id):
        task = get_db().execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            abort(404)
        return task

    def validate_task_form():
        values = {
            "title": request.form.get("title", "").strip(),
            "description": request.form.get("description", "").strip(),
            "due_date": request.form.get("due_date", "").strip(),
            "priority": request.form.get("priority", ""),
            "status": request.form.get("status", ""),
            "assignee": request.form.get("assignee", "").strip(),
        }
        errors = []
        if not values["title"]:
            errors.append("Укажите название задачи.")
        elif len(values["title"]) > 200:
            errors.append("Название не должно превышать 200 символов.")
        if len(values["description"]) > 10000:
            errors.append("Описание не должно превышать 10 000 символов.")
        if len(values["assignee"]) > 100:
            errors.append("Имя исполнителя не должно превышать 100 символов.")
        if values["priority"] not in PRIORITIES:
            errors.append("Выберите корректный приоритет.")
        if values["status"] not in STATUSES:
            errors.append("Выберите корректный статус.")
        if values["due_date"]:
            try:
                date.fromisoformat(values["due_date"])
            except ValueError:
                errors.append("Укажите корректную планируемую дату.")
        else:
            values["due_date"] = None
        return values, errors

    @app.get("/")
    def index():
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1

        status_filter = request.args.get("status", "")
        if status_filter not in STATUSES:
            status_filter = ""
        priority_filter = request.args.get("priority", "")
        if priority_filter not in PRIORITIES:
            priority_filter = ""
        sort_by = request.args.get("sort", "created_desc")
        if sort_by not in SORT_OPTIONS:
            sort_by = "created_desc"

        conditions = []
        parameters = []
        if status_filter:
            conditions.append("status = ?")
            parameters.append(status_filter)
        if priority_filter:
            conditions.append("priority = ?")
            parameters.append(priority_filter)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        database = get_db()
        total = database.execute(
            f"SELECT COUNT(*) FROM tasks {where_clause}", parameters
        ).fetchone()[0]
        page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, page_count)
        tasks = database.execute(
            f"SELECT * FROM tasks {where_clause} ORDER BY {SORT_OPTIONS[sort_by]} "
            "LIMIT ? OFFSET ?",
            (*parameters, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
        return render_template(
            "index.html",
            tasks=tasks,
            page=page,
            page_count=page_count,
            total=total,
            status_filter=status_filter,
            priority_filter=priority_filter,
            sort_by=sort_by,
        )

    @app.route("/tasks/new", methods=("GET", "POST"))
    def create_task():
        values = {
            "title": "",
            "description": "",
            "due_date": date.today().isoformat(),
            "priority": "medium",
            "status": "new",
            "assignee": "Все",
        }
        if request.method == "POST":
            values, errors = validate_task_form()
            if not errors:
                get_db().execute(
                    """
                    INSERT INTO tasks (title, description, due_date, priority, status, assignee)
                    VALUES (:title, :description, :due_date, :priority, :status, :assignee)
                    """,
                    values,
                )
                get_db().commit()
                flash("Задача создана.", "success")
                return redirect(url_for("index"))
            for error in errors:
                flash(error, "error")
        return render_template("task_form.html", task=values, page_title="Новая задача")

    @app.get("/tasks/<int:task_id>")
    def task_detail(task_id):
        task = get_task(task_id)
        comments = get_db().execute(
            "SELECT * FROM comments WHERE task_id = ? ORDER BY created_at, id",
            (task_id,),
        ).fetchall()
        return render_template("task_detail.html", task=task, comments=comments)

    @app.route("/tasks/<int:task_id>/edit", methods=("GET", "POST"))
    def edit_task(task_id):
        task = get_task(task_id)
        values = dict(task)
        if request.method == "POST":
            values, errors = validate_task_form()
            if not errors:
                get_db().execute(
                    """
                    UPDATE tasks
                    SET title = :title, description = :description, due_date = :due_date,
                        priority = :priority, status = :status, assignee = :assignee
                    WHERE id = :id
                    """,
                    {**values, "id": task_id},
                )
                get_db().commit()
                flash("Изменения сохранены.", "success")
                return redirect(url_for("task_detail", task_id=task_id))
            for error in errors:
                flash(error, "error")
        return render_template("task_form.html", task=values, page_title="Редактирование задачи")

    @app.post("/tasks/<int:task_id>/status")
    def update_task_status(task_id):
        get_task(task_id)
        status = request.form.get("status", "")
        if status not in STATUSES:
            flash("Выберите корректный статус.", "error")
        else:
            get_db().execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, task_id)
            )
            get_db().commit()
            flash("Статус задачи обновлён.", "success")

        try:
            page = max(1, int(request.form.get("page", "1")))
        except ValueError:
            page = 1
        status_filter = request.form.get("filter_status", "")
        priority_filter = request.form.get("filter_priority", "")
        sort_by = request.form.get("sort", "created_desc")
        return redirect(
            url_for(
                "index",
                page=page,
                status=status_filter if status_filter in STATUSES else None,
                priority=priority_filter if priority_filter in PRIORITIES else None,
                sort=sort_by
                if sort_by in SORT_OPTIONS and sort_by != "created_desc"
                else None,
            )
        )

    @app.post("/tasks/<int:task_id>/comments")
    def add_comment(task_id):
        get_task(task_id)
        author = request.form.get("author", "").strip()
        body = request.form.get("body", "").strip()
        if len(author) > 100:
            flash("Имя автора не должно превышать 100 символов.", "error")
        elif not body:
            flash("Комментарий не может быть пустым.", "error")
        elif len(body) > 5000:
            flash("Комментарий не должен превышать 5 000 символов.", "error")
        else:
            get_db().execute(
                "INSERT INTO comments (task_id, author, body) VALUES (?, ?, ?)",
                (task_id, author, body),
            )
            get_db().commit()
            flash("Комментарий добавлен.", "success")
        return redirect(url_for("task_detail", task_id=task_id))

    @app.post("/tasks/<int:task_id>/delete")
    def delete_task(task_id):
        get_task(task_id)
        get_db().execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        get_db().commit()
        flash("Задача удалена.", "success")
        status_filter = request.form.get("filter_status", "")
        priority_filter = request.form.get("filter_priority", "")
        sort_by = request.form.get("sort", "created_desc")
        return redirect(
            url_for(
                "index",
                status=status_filter if status_filter in STATUSES else None,
                priority=priority_filter if priority_filter in PRIORITIES else None,
                sort=sort_by
                if sort_by in SORT_OPTIONS and sort_by != "created_desc"
                else None,
            )
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
