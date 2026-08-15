import uuid


def new_public_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex}'
