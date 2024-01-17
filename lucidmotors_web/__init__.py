from flask import Flask, render_template, session, redirect, url_for, request

from typing import Any
from google.protobuf.unknown_fields import UnknownFieldSet
from grpc.aio import ClientCallDetails, UnaryUnaryCall
from hashlib import sha1
from pathlib import Path

import os
import sys
import uuid
import json
import grpc
import grpc.aio
import time
import gzip
import aiohttp
import asyncio
import google._upb
import google.protobuf

from lucidmotors.gen import login_session_pb2
from lucidmotors.gen import login_session_pb2_grpc

from lucidmotors.gen import user_profile_service_pb2
from lucidmotors.gen import user_profile_service_pb2_grpc

from lucidmotors.gen import trip_service_pb2
from lucidmotors.gen import trip_service_pb2_grpc

from lucidmotors.gen import vehicle_state_service_pb2
from lucidmotors.gen import vehicle_state_service_pb2_grpc

from lucidmotors.gen import salesforce_service_pb2
from lucidmotors.gen import salesforce_service_pb2_grpc

from lucidmotors.gen import charging_service_pb2
from lucidmotors.gen import charging_service_pb2_grpc

app = Flask(__name__)

app.secret_key = os.getenv("LUCIDMOTORS_WEB_SECRET", None)
if app.secret_key is None:
    app.logger.error("LUCIDMOTORS_WEB_SECRET must be set in environment")
    sys.exit(1)

wire_types = {
    0: 'varint',
    1: 'fixed-64bit',
    2: 'length-delimited',
    3: 'group-start',
    4: 'group-end',
    5: 'fixed-32bit',
}

# Fields to be hidden in gRPC response data
sensitive_fields = {
    'uid',
    'id_token',
    'refresh_token',
    'gigya_jwt',
    'expiry_time_sec', # not really sensitive, just unnecessary
    'email',
    'username',
    'first_name',
    'last_name',
    'vehicle_id',
    'vin',
    'ema_id',
    'latitude',
    'longitude',
}

def grpc_dump_recursive(message: Any, depth: int = 0) -> str:
    response = ''

    if isinstance(message, (google._upb._message.RepeatedScalarContainer, google._upb._message.RepeatedCompositeContainer)):
        for elem in message:
            response += grpc_dump_recursive(elem, depth=depth)
        return response

    if not isinstance(message, google.protobuf.message.Message):
        return response

    indent = ' ' * depth
    response += f'{indent}{type(message)}:\n'

    depth += 1
    indent = ' ' * depth

    for field in UnknownFieldSet(message):
        wire_type = wire_types[field.wire_type]
        response += f'{indent}Unknown field {field.field_number} wire type {wire_type}: {field.data!r}\n'

    for descriptor, field in message.ListFields():
        if isinstance(field, (google.protobuf.message.Message, google._upb._message.RepeatedScalarContainer, google._upb._message.RepeatedCompositeContainer)):
            field_desc_short = ''
        elif descriptor.enum_type is not None:
            # TODO: Handle a list of enum, like LoginResponse.subscriptions
            enum = descriptor.enum_type
            if field in enum.values_by_number:
                name = enum.values_by_number[field].name
                field_desc_short = f'{name} ({field})'
            else:
                field_desc_short = f'UNKNOWN ENUMERATOR: {field}'
        else:
            field_desc_short = str(field)
        if descriptor.name in sensitive_fields:
            field_desc_short = '[removed]'
        response += f'{indent}Field {descriptor.number}, {descriptor.name}: {field_desc_short}\n'
        response += grpc_dump_recursive(field, depth=depth)

    return response


async def grpc_dump_user_vehicles(username: str, password: str) -> str:
    cmgr = grpc.aio.secure_channel("mobile.deneb.prod.infotainment.pdx.atieva.com",
                                   grpc.ssl_channel_credentials())

    async with cmgr as channel:
        login_service = login_session_pb2_grpc.LoginSessionStub(channel)

        device_id = f'{uuid.getnode():x}'
        req = login_session_pb2.LoginRequest(
            username=username,
            password=password,
            notification_channel_type=login_session_pb2.NotificationChannelType.NOTIFICATION_CHANNEL_ONE,
            notification_device_token=device_id,
            os=login_session_pb2.Os.OS_IOS,
            locale='en_US',
            client_name='python-lucidmotors',
            device_id=device_id,
        )

        response = await login_service.Login(req)
        text = grpc_dump_recursive(response)

    return text

async def json_login_request(username: str, password: str) -> Any:
    request = {
        "username": username,
        "password": password,
        "os": 1,
        "notification_channel_type": 1,
        "notification_device_token": "1234",
        "locale": "en_US",
        "device_id": "python-lucidmotors",
    }

    headers = {
        "user-agent": f"python-lucidmotors/0.1.1",
    }

    cmgr = aiohttp.ClientSession("https://mobile.deneb.prod.infotainment.pdx.atieva.com",
                                 headers=headers)

    async with cmgr as session:
        async with session.post("/v1/login", json=request) as resp:
            raw = await resp.json()

    raw['uid'] = '[removed]'
    raw['sessionInfo']['idToken'] = '[removed]'
    raw['sessionInfo']['refreshToken'] = '[removed]'
    raw['sessionInfo']['gigyaJwt'] = '[removed]'
    raw['sessionInfo']['expiryTimeSec'] = '[removed]'
    raw['sessionInfo']['jwtToken'] = '[removed]'

    raw['userProfile']['email'] = '[removed]'
    raw['userProfile']['username'] = '[removed]'
    raw['userProfile']['firstName'] = '[removed]'
    raw['userProfile']['lastName'] = '[removed]'
    raw['userProfile']['emaId'] = '[removed]'

    for i in range(len(raw['userVehicleData'])):
        raw['userVehicleData'][i]['vehicleId'] = '[removed]'
        raw['userVehicleData'][i]['vehicleConfig']['vin'] = '[removed]'
        raw['userVehicleData'][i]['vehicleConfig']['emaId'] = '[removed]'
        raw['userVehicleData'][i]['vehicleConfig']['chargingAccounts'][0][
            'emaid'
        ] = '[removed]'
        raw['userVehicleData'][i]['vehicleConfig']['chargingAccounts'][0][
            '[removed]'
        ] = '[removed]'
        raw['userVehicleData'][i]['vehicleState']['gps']['location'][
            'latitude'
        ] = '[removed]'
        raw['userVehicleData'][i]['vehicleState']['gps']['location'][
            'longitude'
        ] = '[removed]'

    return raw

@app.route("/", methods=["GET", "POST"])
async def index():
    if request.method == "POST":
        errors = []
        email = request.form.get('email', None)
        password = request.form.get('password', None)

        if email is None:
            errors.append("Email address is a required field")
        if password is None:
            errors.append("Password is a required field")

        if errors:
            return render_template("login.html", errors=errors)

        grpc_text = await grpc_dump_user_vehicles(email, password)

        await asyncio.sleep(0.5)

        raw_json = await json_login_request(email, password)
        pretty_json = json.dumps(raw_json, indent=2)

        return render_template(
            "login_response.html",
            grpc_text=grpc_text,
            pretty_json=pretty_json,
        )

    return render_template("login.html")

submissions_dir = Path('./submissions')
@app.route("/submit", methods=["POST"])
def submit():
    grpc = request.form.get('grpc', None)
    jsn = request.form.get('json', None)

    errors = []

    if grpc is None:
        errors.append("Missing grpc data")
    if jsn is None:
        errors.append("Missing json data")

    if errors:
        return render_template("login.html", errors=errors)

    data = grpc + '\n\n\n' + jsn
    data_bytes = data.encode('utf-8', errors='replace')
    sha = sha1(data_bytes).hexdigest()

    app.logger.info(f'Submission from {request.remote_addr}: {sha}')

    filepath = submissions_dir / f'submission.{sha}.gz'
    if not filepath.exists():
        with gzip.open(filepath, 'w') as f:
            f.write(data_bytes)

    return render_template("thanks.html")
