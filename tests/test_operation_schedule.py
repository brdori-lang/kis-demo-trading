import pytest
from pydantic import ValidationError
from app.main import OperationSettingsRequest
from operations import OperationsService
from lab_repository import SQLiteLabRepository


def test_default_schedule_can_be_saved_through_api_schema(tmp_path):
    service = OperationsService(SQLiteLabRepository(tmp_path / 'schedule.db'))
    request = OperationSettingsRequest(**service.get_settings())
    saved = service.save_settings(request.model_dump(exclude_none=True))
    assert saved['analysis_schedule'] == '0 18 * * 1-5'


@pytest.mark.parametrize('schedule', ['18:00', '0 18 * * 1-5', '*/15 9-15 * * 1-5'])
def test_supported_schedule_formats(schedule):
    assert OperationSettingsRequest(analysis_schedule=schedule).analysis_schedule == schedule


@pytest.mark.parametrize('schedule', ['25:00', 'hello', '* * *', '<script>alert(1)</script>'])
def test_rejects_invalid_schedule_formats(schedule):
    with pytest.raises(ValidationError):
        OperationSettingsRequest(analysis_schedule=schedule)
