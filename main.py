#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#


import sys

from airbyte_cdk.entrypoint import launch
from source_app_store_full import SourceAppStoreFull

if __name__ == "__main__":
    source = SourceAppStoreFull()
    launch(source, sys.argv[1:])
