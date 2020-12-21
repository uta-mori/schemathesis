import pytest

from schemathesis.models import Endpoint, EndpointDefinition
from schemathesis.specs.openapi.links import get_container
from schemathesis.specs.openapi.parameters import OpenAPI30Parameter


def test_get_container_invalid_location():
    endpoint = Endpoint(
        path="/users/{user_id}",
        method="get",
        schema=None,
        definition=EndpointDefinition(
            raw={},
            resolved={},
            scope="",
            parameters=[
                OpenAPI30Parameter({"in": "query", "name": "code", "type": "integer"}),
                OpenAPI30Parameter({"in": "query", "name": "user_id", "type": "integer"}),
                OpenAPI30Parameter({"in": "query", "name": "common", "type": "integer"}),
            ],
        ),
    )
    case = endpoint.make_case()
    with pytest.raises(ValueError, match="Parameter `unknown` is not defined in endpoint `GET /users/{user_id}`"):
        get_container(case, None, "unknown")
