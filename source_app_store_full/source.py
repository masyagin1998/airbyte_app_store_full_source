#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#


import random
import re
from datetime import datetime
from time import sleep
from typing import Any, Iterable, List, Mapping, MutableMapping, Tuple, Optional

import requests
from airbyte_cdk import AirbyteLogger
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http import HttpStream
from airbyte_cdk.sources.streams.http.auth import NoAuth

URL_BASE = "https://amp-api.apps.apple.com"


class Reviews(HttpStream):
    url_base = URL_BASE
    cursor_field = "ds"
    primary_key = "ds"

    @staticmethod
    def __datetime_to_str(d: datetime) -> str:
        return d.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def __str_to_datetime(d: str) -> datetime:
        return datetime.strptime(d, "%Y-%m-%dT%H:%M:%S")

    def __get_token(self):
        resp = requests.get(self.__url)
        tags = resp.text.splitlines()
        for tag in tags:
            if re.match(r"<meta.+web-experience-app/config/environment", tag):
                token = re.search(r"token%22%3A%22(.+?)%22", tag).group(1)
                return f"bearer {token}"
        return ""

    def __update_params(self):
        self.__request_offset = 0
        self.__base_landing_url = "https://apps.apple.com"
        self.__url = self.__base_landing_url + "/{}/app/{}/id{}".format(
            self.__config["countries"][self.__country_ind], self.__config["app_name"], self.__config["app_id"]
        )
        self.__token = self.__get_token()

    # noinspection PyUnusedLocal
    def __init__(self, config, **kwargs):
        super().__init__()
        self.__config = config

        self.__logger = AirbyteLogger()

        self.__country_ind = 0
        self.__count = 0
        self.__total_count = 0

        self.__update_params()

        self.__user_agents = [
            "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; SV1; .NET CLR 1.1.4322)",
            "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko",
            "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)",
        ]

        self.__cursor_value = self.__str_to_datetime(self.__config["start_time"])
        self.__tmp_cursor_value = self.__str_to_datetime(self.__config["start_time"])

        self.__logger.info("Read latest review timestamp from config: {}".format(self.__config["start_time"]))

    @property
    def state(self) -> Mapping[str, Any]:
        ds = self.__datetime_to_str(self.__tmp_cursor_value)
        self.__logger.info("Saved latest review timestamp to file: {}".format(ds))
        return {self.cursor_field: ds}

    @state.setter
    def state(self, value: Mapping[str, Any]):
        ds = value.get(self.cursor_field)
        if ds is not None:
            self.__logger.info("Read latest review timestamp from file: {}".format(ds))
            self.__cursor_value = self.__str_to_datetime(ds)
            self.__tmp_cursor_value = self.__str_to_datetime(ds)

    def read_records(self, *args, **kwargs) -> Iterable[Mapping[str, Any]]:
        for record in super().read_records(*args, **kwargs):
            ds = record.get("ds")
            if ds is not None:
                self.__tmp_cursor_value = max(self.__tmp_cursor_value, ds)
            yield record

    http_method = "GET"

    def path(
            self,
            *,
            stream_state: Mapping[str, Any] = None,
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> str:
        return "/v1/catalog/{}/apps/{}/reviews".format(
            self.__config["countries"][self.__country_ind],
            self.__config["app_id"],
        )

    def request_params(
            self,
            stream_state: Mapping[str, Any],
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        limit = self.__config.get("max_reviews_per_req")
        if (limit is None) or (limit > 20):
            limit = 20
        return {
            "l": "en-GB",
            "offset": self.__request_offset,
            "limit": limit,
            "platform": "web",
            "additionalPlatforms": "appletv,ipad,iphone,mac",
        }

    def request_headers(
            self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> Mapping[str, Any]:
        return {
            "Accept": "application/json",
            "Authorization": self.__token,
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            # noinspection PyProtectedMember
            "Origin": self.__base_landing_url,
            "Referer": self.__url,
            # noinspection PyProtectedMember
            "User-Agent": random.choice(self.__user_agents),
        }

    @staticmethod
    def __fetch_review_items(response: requests.Response):
        return response.json()["data"]

    @staticmethod
    def __rename_field(v: dict, v_name: str, v1: dict, v1_name: str):
        field = v.get(v_name)
        if field is not None:
            v1[v1_name] = field

    def __transform(self, v: dict):
        va = v.get("attributes", {})
        title = va.get("title")
        review = va.get("review")
        message = ""
        if title is not None:
            message += (title + ". ")
        if review is not None:
            message += review

        v1 = {
            "source": "App Store",
            "lang": "unknown",
            "country": self.__config["countries"][self.__country_ind],
            "version": "unknown",
            "message": message
        }

        self.__rename_field(v, "id", v1, "ticket_id")
        self.__rename_field(va, "date", v1, "ds")
        self.__rename_field(va, "rating", v1, "score")
        self.__rename_field(va, "userName", v1, "user_id")

        v1["ds"] = datetime.strptime(v1["ds"], "%Y-%m-%dT%H:%M:%SZ")

        return v1

    def parse_response(
            self,
            response: requests.Response,
            *,
            stream_state: Mapping[str, Any],
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        result = []
        review_items = self.__fetch_review_items(response)
        for review in review_items:
            v = self.__transform(review)
            ds = v.get("ds")
            if (ds is None) or (ds > self.__cursor_value):
                result.append(v)
        self.__count += len(result)
        return result

    def __fetch_next_page_token(self, response: requests.Response):
        token = "token"
        self.__request_offset = response.json().get("next")
        if self.__request_offset is not None:
            self.__request_offset = re.search("^.+offset=([0-9]+).*$", self.__request_offset).group(1)
            self._request_offset = int(self.__request_offset)
        if self.__request_offset is None:
            self.__logger.info("Fetched {} reviews for country=\"{}\"".format(
                self.__count, self.__config["countries"][self.__country_ind]
            ))
            self.__country_ind += 1
            if self.__country_ind == len(self.__config["countries"]):
                self.__logger.info("Totally fetched {} reviews".format(self.__total_count + self.__count))
                token = None
            else:
                self.__update_params()

            self.__total_count += self.__count
            self.__count = 0
        return token

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        timeout_ms = self.__config.get("timeout_ms")
        if timeout_ms is not None:
            sleep(timeout_ms / 1000.0)
        return self.__fetch_next_page_token(response)

    def should_retry(self, response: requests.Response) -> bool:
        return (response.status_code == 401) or \
               (response.status_code == 429) or \
               (500 <= response.status_code < 600)


class SourceAppStoreFull(AbstractSource):
    @staticmethod
    def __get_app_id(app_name: str) -> [str, None]:
        resp = requests.get("https://www.google.com/search", params={"q": f"app store {app_name}"})
        pattern = fr"https://apps.apple.com/[a-z]{{2}}/.+?/id([0-9]+)"
        app_id = re.search(pattern, resp.text)
        if app_id is not None:
            app_id = app_id.group(1)
        return app_id

    def check_connection(self, logger, config) -> Tuple[bool, any]:
        logger.info("Checking connection configuration for app \"{}\"...".format(config["app_name"]))

        logger.info("Checking \"app_name\"...")
        app_id = self.__get_app_id(config["app_name"])
        if app_id is None:
            error_text = "\"app_name\" \"{}\" is invalid!".format(config["app_name"])
            logger.error(error_text)
            return False, {"key": "app_name", "value": config["app_name"], "error_text": error_text}
        logger.info("\"app_name\" \"{}\" is valid".format(config["app_name"]))

        if config.get("app_id") is not None:
            logger.info("Checking \"app_id\"...")
            if config["app_id"] != app_id:
                error_text = "\"app_id\" \"{}\" is invalid!".format(config["app_id"])
                logger.error(error_text)
                return False, {"key": "app_id", "value": config["app_id"], "error_text": error_text}
            logger.info("\"app_id\" \"{}\" is valid".format(config["app_id"]))

        logger.info("Connection configuration is valid!")
        return True, None

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        auth = NoAuth()
        if config.get("app_id") is None:
            config["app_id"] = self.__get_app_id(config["app_name"])
        return [Reviews(authenticator=auth, config=config)]
