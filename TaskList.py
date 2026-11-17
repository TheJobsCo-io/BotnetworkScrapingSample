"""Core logic for a task list. All behavior lives on the class."""

MAX_TITLE_LENGTH = 80


class TaskList:
    def __init__(self):
        self._tasks = []

    def add_task(self, title):
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Task title cannot be empty.")
        if len(clean_title) > MAX_TITLE_LENGTH:
            raise ValueError("Task title is too long.")
        self._tasks.append(clean_title)

    def show_tasks(self):
        if not self._tasks:
            print("No tasks yet.")
            return

        print("Tasks:")
        for number, task_title in enumerate(self._tasks, start=1):
            print(f"{number}. {task_title}")
