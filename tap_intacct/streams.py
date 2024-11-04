"""Stream type classes for tap-intacct."""

from __future__ import annotations

import json
import typing as t
import uuid
from datetime import datetime, timezone

import xmltodict
from singer_sdk.pagination import BaseAPIPaginator  # noqa: TCH002
from singer_sdk.streams import RESTStream

from tap_intacct.const import GET_BY_DATE_FIELD, KEY_PROPERTIES, REP_KEYS
from tap_intacct.exceptions import (
    AuthFailure,
    BadGatewayError,
    ExpiredTokenError,
    InternalServerError,
    InvalidRequest,
    InvalidTokenError,
    InvalidXmlResponse,
    NoPrivilegeError,
    NotFoundItemError,
    OfflineServiceError,
    PleaseTryAgainLaterError,
    RateLimitError,
    SageIntacctSDKError,
    WrongParamsError,
)

if t.TYPE_CHECKING:
    import requests
    from singer_sdk.helpers.types import Context


class IntacctStream(RESTStream):
    """Intacct stream class."""

    # Update this value if necessary or override `parse_response`.
    rest_method = "POST"
    path = None

    def __init__(
        self,
        *args,
        intacct_obj_name=None,
        replication_key=None,
        sage_client=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.primary_key = KEY_PROPERTIES[self.name]
        self.intacct_obj_name = intacct_obj_name
        self.replication_key = replication_key
        self.sage_client = sage_client
        self.session_id = sage_client._SageIntacctSDK__session_id
        self.datetime_fields = [
            i
            for i, t in self.schema["properties"].items()
            if t.get("format") == "date-time"
        ]

    @property
    def url_base(self) -> str:
        """Return the API URL root, configurable via tap settings."""
        return self.config["api_url"]

    @property
    def http_headers(self) -> dict:
        """Return the http headers needed.

        Returns:
            A dictionary of HTTP headers.
        """
        headers = {"content-type": "application/xml"}
        if "user_agent" in self.config:
            headers["User-Agent"] = self.config.get("user_agent")
        # If not using an authenticator, you may also provide inline auth headers:
        # headers["Private-Token"] = self.config.get("auth_token")  # noqa: ERA001
        return headers

    def get_new_paginator(self) -> BaseAPIPaginator:
        """Create a new pagination helper instance.

        If the source API can make use of the `next_page_token_jsonpath`
        attribute, or it contains a `X-Next-Page` header in the response
        then you can remove this method.

        If you need custom pagination that uses page numbers, "next" links, or
        other approaches, please read the guide: https://sdk.meltano.com/en/v0.25.0/guides/pagination-classes.html.

        Returns:
            A pagination helper instance.
        """
        return super().get_new_paginator()

    def _format_date_for_intacct(self, datetime: datetime) -> str:
        """Intacct expects datetimes in a 'MM/DD/YY HH:MM:SS' string format.

        Args:
            datetime: The datetime to be converted.

        Returns:
            'MM/DD/YY HH:MM:SS' formatted string.
        """
        return datetime.strftime("%m/%d/%Y %H:%M:%S")

    def prepare_request(
        self,
        context: Context | None,
        next_page_token: str | None,
    ) -> requests.PreparedRequest:
        """Prepare a request object for this stream.

        If partitioning is supported, the `context` object will contain the partition
        definitions. Pagination information can be parsed from `next_page_token` if
        `next_page_token` is not None.

        Args:
            context: Stream partition or context dictionary.
            next_page_token: Token, page number or any request argument to request the
                next page of data.

        Returns:
            Build a request with the stream's URL, path, query parameters,
            HTTP headers and authenticator.
        """
        http_method = self.rest_method
        url: str = self.get_url(context)
        params: dict | str = self.get_url_params(context, next_page_token)
        request_data = self.prepare_request_payload(context, next_page_token)
        headers = self.http_headers

        return self.build_prepared_request(
            method=http_method,
            url=url,
            params=params,
            headers=headers,
            # Note: Had to override this method to switch this to data instead of json
            data=request_data,
        )

    def prepare_request_payload(
        self,
        context: Context | None,  # noqa: ARG002
        next_page_token: t.Any | None,  # noqa: ARG002, ANN401
    ) -> dict | None:
        """Prepare the data payload for the REST API request.

        By default, no payload will be sent (return None).

        Args:
            context: The stream context.
            next_page_token: The next page index or value.

        Returns:
            A dictionary with the JSON body for a POST requests.
        """
        if self.name == "audit_history":
            raise Exception("TODO hanlde audit streams")

        rep_key = REP_KEYS.get(self.name, GET_BY_DATE_FIELD)
        query_filter = {
            "greaterthanorequalto": {
                "field": rep_key,
                "value": self._format_date_for_intacct(
                    self.get_starting_timestamp(context)
                ),
            }
        }
        orderby = {
            "order": {
                "field": rep_key,
                "ascending": {},
            }
        }
        data = {
            "query": {
                "object": self.intacct_obj_name,
                "select": {"field": list(self.schema["properties"])},
                "options": {"showprivate": "true"},
                "filter": query_filter,
                "pagesize": 1000,
                # TODO: need to paginate here
                "offset": 0,
                "orderby": orderby,
            }
        }
        key = next(iter(data))
        timestamp = datetime.now(timezone.utc)
        dict_body = {
            "request": {
                "control": {
                    "senderid": self.config["sender_id"],
                    "password": self.config["sender_password"],
                    "controlid": timestamp,
                    "uniqueid": False,
                    "dtdversion": 3.0,
                    "includewhitespace": False,
                },
                "operation": {
                    "authentication": {"sessionid": self.session_id},
                    "content": {
                        "function": {"@controlid": str(uuid.uuid4()), key: data[key]}
                    },
                },
            }
        }
        return xmltodict.unparse(dict_body)

    def parse_response(self, response: requests.Response) -> t.Iterable[dict]:
        """Parse the response and return an iterator of result records.

        Args:
            response: The HTTP ``requests.Response`` object.

        Yields:
            Each record from the source.
        """
        try:
            parsed_xml = xmltodict.parse(response.text)
            parsed_response = json.loads(json.dumps(parsed_xml))
        except:
            if response.status_code == 502:
                raise BadGatewayError(
                    f"Response status code: {response.status_code}, response: {response.text}"
                )
            if response.status_code == 503:
                raise OfflineServiceError(
                    f"Response status code: {response.status_code}, response: {response.text}"
                )
            if response.status_code == 429:
                raise RateLimitError(
                    f"Response status code: {response.status_code}, response: {response.text}"
                )
            raise InvalidXmlResponse(
                f"Response status code: {response.status_code}, response: {response.text}"
            )

        if response.status_code == 200:
            if parsed_response["response"]["control"]["status"] == "success":
                api_response = parsed_response["response"]["operation"]

            if parsed_response["response"]["control"]["status"] == "failure":
                exception_msg = self.sage_client.decode_support_id(
                    parsed_response["response"]["errormessage"]
                )
                raise WrongParamsError(
                    "Some of the parameters are wrong", exception_msg
                )

            if api_response["authentication"]["status"] == "failure":
                raise InvalidTokenError(
                    "Invalid token / Incorrect credentials",
                    api_response["errormessage"],
                )

            if api_response["result"]["status"] == "success":
                return api_response["result"]["data"][self.intacct_obj_name]

            self.logger.error(f"Intacct error response: {api_response}")
            error = (
                api_response.get("result", {}).get("errormessage", {}).get("error", {})
            )
            desc_2 = (
                error.get("description2")
                if isinstance(error, dict)
                else error[0].get("description2")
                if isinstance(error, list) and error
                else ""
            )
            # if (
            #     api_response['result']['status'] == 'failure'
            #     and error
            #     and "There was an error processing the request"
            #     in desc_2
            #     and dict_body["request"]["operation"]["content"]["function"]["query"][
            #         "object"
            #     ]
            #     == "AUDITHISTORY"
            # ):
            #     return {"result": "skip_and_paginate"}

        exception_msg = (
            parsed_response.get("response", {}).get("errormessage", {}).get("error", {})
        )
        correction = exception_msg.get("correction", {})

        if response.status_code == 400:
            if exception_msg.get("errorno") == "GW-0011":
                raise AuthFailure(
                    f"One or more authentication values are incorrect. Response:{parsed_response}"
                )
            raise InvalidRequest("Invalid request", parsed_response)

        if response.status_code == 401:
            raise InvalidTokenError(
                f"Invalid token / Incorrect credentials. Response: {parsed_response}"
            )

        if response.status_code == 403:
            raise NoPrivilegeError(
                f"Forbidden, the user has insufficient privilege. Response: {parsed_response}"
            )

        if response.status_code == 404:
            raise NotFoundItemError(
                f"Not found item with ID. Response: {parsed_response}"
            )

        if response.status_code == 498:
            raise ExpiredTokenError(
                f"Expired token, try to refresh it. Response: {parsed_response}"
            )

        if response.status_code == 500:
            raise InternalServerError(
                f"Internal server error. Response: {parsed_response}"
            )

        if correction and "Please Try Again Later" in correction:
            raise PleaseTryAgainLaterError(parsed_response)

        raise SageIntacctSDKError("Error: {0}".format(parsed_response))

    def _parse_to_datetime(self, date_str: str) -> datetime:
        # Try to parse with the full format first
        try:
            return datetime.strptime(date_str, "%m/%d/%Y %H:%M:%S")
        # .replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            # If it fails, try the date-only format
            try:
                return datetime.strptime(date_str, "%m/%d/%Y")
            # .replace(tzinfo=datetime.timezone.utc)
            except ValueError as err:
                # Handle cases where the format is still incorrect
                msg = f"Invalid date format: {date_str}"
                raise ValueError(msg) from err

    def post_process(
        self,
        row: dict,
        context: Context | None = None,  # noqa: ARG002
    ) -> dict | None:
        """As needed, append or transform raw data to match expected structure.

        Args:
            row: An individual record from the stream.
            context: The stream context.

        Returns:
            The updated record dictionary, or ``None`` to skip the record.
        """
        for field in self.datetime_fields:
            if row[field] is not None:
                row[field] = self._parse_to_datetime(row[field])
        return row
