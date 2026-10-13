from __future__ import annotations

import unittest

from PySide6.QtCore import QCoreApplication, QObject, QPersistentModelIndex, Signal

from app.core.download_service import DownloadTask
from app.presentation.task_list_model import TaskListModel


class _Service(QObject):
    task_added = Signal(object)
    tasks_added = Signal(object)
    task_updated = Signal(object)
    task_progress = Signal(str, object)
    task_deleted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.tasks: dict[str, DownloadTask] = {}


class _CountingTaskListModel(TaskListModel):
    def __init__(self, service: _Service) -> None:
        self.order_requests = 0
        super().__init__(service)

    def _ordered_task_ids(self) -> list[str]:
        self.order_requests += 1
        return super()._ordered_task_ids()


class TaskListModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_queued_burst_materializes_every_task(self) -> None:
        service = _Service()
        model = TaskListModel(service)
        tasks = [
            DownloadTask(
                id=f"burst-{index}",
                url=f"https://example.test/{index}",
                output_dir="downloads",
                title=f"Burst {index}",
                created_at=f"2026-09-10T12:00:{index:02d}",
            )
            for index in range(6)
        ]
        service.tasks = {task.id: task for task in tasks}

        for task in tasks:
            service.task_added.emit(task)
        for _ in range(4):
            self.app.processEvents()

        self.assertEqual(model.rowCount(), len(tasks))
        self.assertEqual(
            {model.taskIdAt(row) for row in range(model.rowCount())},
            {task.id for task in tasks},
        )

    def test_progress_without_sort_change_does_not_reorder_every_task(self) -> None:
        service = _Service()
        task = DownloadTask(
            id="active",
            url="https://example.test/active",
            output_dir="downloads",
            title="Active",
            status="downloading",
            created_at="2026-09-10T12:00:00",
        )
        service.tasks = {task.id: task}
        model = _CountingTaskListModel(service)
        initial_requests = model.order_requests

        service.task_progress.emit(task.id, {"downloaded_bytes": 1})
        self.app.processEvents()

        self.assertEqual(model.order_requests, initial_requests)
        task.status = "completed"
        service.task_progress.emit(task.id, {"status": "finished"})
        self.app.processEvents()
        self.assertEqual(model.order_requests, initial_requests + 1)

    def test_processing_queue_filter_uses_real_waiting_stage(self) -> None:
        service = _Service()
        waiting = DownloadTask(id="waiting", url="https://example.test/wait", output_dir="downloads", status="processing", stage="waiting_processing")
        running = DownloadTask(id="running", url="https://example.test/run", output_dir="downloads", status="processing", stage="transcoding")
        service.tasks = {task.id: task for task in (waiting, running)}
        model = TaskListModel(service)
        model.setStatusFilter("waiting_processing")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.taskIdAt(0), waiting.id)
        self.assertEqual(model.index(0, 0).data(model.StatusRole), "waiting_processing")
        waiting.stage = "transcoding"
        service.task_progress.emit(waiting.id, {})
        self.app.processEvents()
        self.assertEqual(model.rowCount(), 0)
        model.setStatusFilter("processing")
        self.assertEqual(model.rowCount(), 2)

    def test_filter_changes_preserve_surviving_rows(self) -> None:
        service = _Service()
        tasks = [
            DownloadTask(
                id=f"task-{index}",
                url=f"https://example.test/{index}",
                output_dir="downloads",
                title=title,
                status=status,
                created_at=f"2026-09-10T12:00:{index:02d}",
            )
            for index, (title, status) in enumerate([
                ("red", "completed"),
                ("blue", "downloading"),
                ("red blue", "completed"),
                ("green", "paused"),
                ("blue red", "failed"),
                ("other", "completed"),
            ])
        ]
        service.tasks = {task.id: task for task in tasks}
        model = TaskListModel(service)
        resets = []
        model.modelReset.connect(lambda: resets.append(True))

        for setter, value, expected in [
            (model.setFilterText, "red", [4, 2, 0]),
            (model.setFilterText, "blue", [1, 4, 2]),
            (model.setStatusFilter, "failed", [4]),
            (model.setFilterText, "", [4]),
            (model.setStatusFilter, "active", [1]),
            (model.setStatusFilter, "all", [1, 5, 4, 3, 2, 0]),
            (model.setFilterText, "absent", []),
            (model.setFilterText, "", [1, 5, 4, 3, 2, 0]),
        ]:
            with self.subTest(filter=value, expected=expected):
                previous = {
                    model.taskIdAt(row): QPersistentModelIndex(model.index(row, 0))
                    for row in range(model.rowCount())
                }
                setter(value)
                expected_ids = [f"task-{index}" for index in expected]
                self.assertEqual(
                    [model.taskIdAt(row) for row in range(model.rowCount())],
                    expected_ids,
                )
                self.assertEqual(resets, [])
                for task_id, index in previous.items():
                    self.assertEqual(index.isValid(), task_id in expected_ids)
                    if index.isValid():
                        self.assertEqual(index.data(model.TaskIdRole), task_id)
                        self.assertEqual(index.row(), expected_ids.index(task_id))

    def test_filter_refresh_handles_a_pending_status_reorder(self) -> None:
        service = _Service()
        tasks = [
            DownloadTask(
                id=f"task-{index}",
                url=f"https://example.test/{index}",
                output_dir="downloads",
                status="completed",
                created_at=f"2026-09-10T12:00:{index:02d}",
            )
            for index in range(3)
        ]
        service.tasks = {task.id: task for task in tasks}
        model = TaskListModel(service)
        tasks[0].status = "downloading"
        service.task_updated.emit(tasks[0])

        model.setFilterText("example.test")
        self.app.processEvents()
        self.assertEqual([model.taskIdAt(row) for row in range(3)], ["task-0", "task-2", "task-1"])

        model.setSortMode("oldest")
        self.assertEqual([model.taskIdAt(row) for row in range(3)], ["task-0", "task-1", "task-2"])

    def test_requested_role_does_not_materialize_other_task_fields(self) -> None:
        class RoleProbe:
            id = "task-id"

            def __getattr__(self, name: str) -> object:
                raise AssertionError(f"unrequested field was read: {name}")

        model = TaskListModel(_Service())
        self.assertEqual(model._role_value(RoleProbe(), model.TaskIdRole), "task-id")


if __name__ == "__main__":
    unittest.main()
