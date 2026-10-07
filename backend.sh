#!/bin/bash

cd src
uvicorn backend.api:app --reload
