"""
Monkey-patches for a2a-sdk 1.0.2 + protobuf >=5.x compatibility.

a2a-sdk 1.0.2 has a known issue (tracked in a2a-python #1011): the
validate_proto_required_fields helpers use the deprecated field.label
attribute which was removed from the protobuf C extension in protobuf 5+.
The SDK code already has TODO comments to replace it with field.is_repeated
once the minimum protobuf version is bumped; this patch applies that fix now.
"""
from __future__ import annotations

import a2a.utils.proto_utils as _proto_utils
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message as ProtobufMessage


def _patched_check_required_field_violation(
    msg: ProtobufMessage, field: FieldDescriptor
) -> _proto_utils.ValidationDetail | None:
    val = getattr(msg, field.name)
    if field.is_repeated:
        if not val:
            return _proto_utils.ValidationDetail(
                field=field.name,
                message='Field must contain at least one element.',
            )
    elif field.has_presence:
        if not msg.HasField(field.name):
            return _proto_utils.ValidationDetail(
                field=field.name, message='Field is required.'
            )
    elif val == field.default_value:
        return _proto_utils.ValidationDetail(field=field.name, message='Field is required.')
    return None


def _patched_recurse_validation(
    msg: ProtobufMessage, field: FieldDescriptor
) -> list[_proto_utils.ValidationDetail]:
    errors: list[_proto_utils.ValidationDetail] = []
    if field.type != FieldDescriptor.TYPE_MESSAGE:
        return errors

    val = getattr(msg, field.name)
    if not field.is_repeated:
        if msg.HasField(field.name):
            sub_errs = _proto_utils._validate_proto_required_fields_internal(val)
            _proto_utils._append_nested_errors(errors, field.name, sub_errs)
    elif field.message_type.GetOptions().map_entry:
        for k, v in val.items():
            if isinstance(v, ProtobufMessage):
                sub_errs = _proto_utils._validate_proto_required_fields_internal(v)
                _proto_utils._append_nested_errors(errors, f'{field.name}[{k}]', sub_errs)
    else:
        for i, item in enumerate(val):
            sub_errs = _proto_utils._validate_proto_required_fields_internal(item)
            _proto_utils._append_nested_errors(errors, f'{field.name}[{i}]', sub_errs)
    return errors


def apply() -> None:
    """Apply the patches. Safe to call multiple times."""
    # Only patch if the C-extension FieldDescriptor lacks the 'label' instance attribute.
    from a2a.types.a2a_pb2 import SendMessageRequest
    sample_field = next(iter(SendMessageRequest.DESCRIPTOR.fields))
    if hasattr(sample_field, 'label'):
        return  # Pure-Python protobuf or future SDK version — no patch needed.

    _proto_utils._check_required_field_violation = _patched_check_required_field_violation
    _proto_utils._recurse_validation = _patched_recurse_validation
