import json
import subprocess
import boto3
import pytest
import os


from lumigo_tracer.tracer import lumigo_tracer
from lumigo_tracer.spans_container import SpansContainer
from lumigo_tracer.lumigo_utils import md5hash

TOKEN = "t_10faa5e13e7844aaa1234"

DEFAULT_USER = "cicd"


@pytest.fixture(scope="session", autouse=True)
def serverless_yaml():
    subprocess.check_output(
        ["sls", "deploy", "--env", os.environ.get("USER", DEFAULT_USER)], cwd="test/"
    )


@pytest.fixture(autouse=True)
def aws_env_variables(monkeypatch):
    """
    When running in AWS Lambda, there are some environment variables that AWS creates and the tracer uses.
    This fixture creates those environment variables.
    """
    monkeypatch.setenv(
        "_X_AMZN_TRACE_ID",
        "RequestId: 4365921c-fc6d-4745-9f00-9fe9c516ede5 Root=1-000044d4-c3881e0c19c02c5e6ffa8f9e;Parent=37cf579525dfb3ba;Sampled=0",
    )


@pytest.fixture
def region():
    return boto3.session.Session().region_name


@pytest.fixture
def account_id():
    return boto3.client("sts").get_caller_identity().get("Account")


@pytest.fixture
def ddb_resource(region):
    return "component-test"


@pytest.fixture
def sns_resource(region, account_id):
    return f"arn:aws:sns:{region}:{account_id}:component-test"


@pytest.fixture
def lambda_resource():
    return "component-test"


@pytest.fixture
def kinesis_resource(region):
    return "component-test"


@pytest.fixture
def sqs_resource(region, account_id):
    return f"https://sqs.{region}.amazonaws.com/{account_id}/component-test"


@pytest.fixture
def s3_bucket_resource():
    return f"python-tracer-component-test-{os.environ.get('USER', DEFAULT_USER)}-s3-bucket"


@pytest.mark.slow
def test_dynamo_db(ddb_resource, region, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        boto3.resource("dynamodb", region_name=region).Table(ddb_resource).put_item(
            Item={"key": "1"}
        )

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    assert len(events) == 1
    assert events[0]["info"]["httpInfo"]["host"] == f"dynamodb.{region}.amazonaws.com"
    assert events[0]["info"]["resourceName"] == ddb_resource
    assert events[0]["info"].get("messageId") == md5hash({"key": {"S": "1"}})
    assert "ended" in events[0]


@pytest.mark.slow
def test_sns(sns_resource, region, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        boto3.resource("sns").Topic(sns_resource).publish(Message=json.dumps({"test": "test"}))

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    assert len(events) == 1
    assert events[0]["info"]["httpInfo"]["host"] == f"sns.{region}.amazonaws.com"
    assert events[0]["info"]["resourceName"] == sns_resource
    assert events[0]["info"]["messageId"]


@pytest.mark.slow
def test_lambda(lambda_resource, region, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        boto3.client("lambda").invoke(
            FunctionName=lambda_resource, InvocationType="Event", Payload=b"null"
        )

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    assert len(events) == 1
    assert events[0]["info"]["httpInfo"]["host"] == f"lambda.{region}.amazonaws.com"
    assert events[0]["info"]["resourceName"] == lambda_resource
    expected_uri = (
        f"lambda.{region}.amazonaws.com/2015-03-31/functions/{lambda_resource}/invocations"
    )
    assert events[0]["info"]["httpInfo"]["request"]["uri"] == expected_uri
    assert events[0]["info"]["messageId"]
    assert events[0].get("id").count("-") == 4


@pytest.mark.slow
def test_kinesis(kinesis_resource, region, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        client = boto3.client("kinesis")
        client.put_record(StreamName=kinesis_resource, Data=b"my data", PartitionKey="1")
        client.put_records(
            StreamName=kinesis_resource,
            Records=[
                {"Data": "First", "PartitionKey": "1"},
                {"Data": "Second", "PartitionKey": "1"},
            ],
        )

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    assert len(events) == 2
    # Single message.
    assert events[0]["info"]["httpInfo"]["host"] == f"kinesis.{region}.amazonaws.com"
    assert events[0]["info"]["resourceName"] == kinesis_resource
    assert events[0]["info"]["messageId"]
    # No scrubbing for PartitionKey
    assert json.loads(events[0]["info"]["httpInfo"]["request"]["body"])["PartitionKey"] == "1"
    # Batch messages.
    assert events[1]["info"]["httpInfo"]["host"] == f"kinesis.{region}.amazonaws.com"
    assert events[1]["info"]["resourceName"] == kinesis_resource
    assert events[1]["info"]["messageId"]


@pytest.mark.slow
def test_sqs(sqs_resource, region, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        client = boto3.client("sqs")
        client.send_message(QueueUrl=sqs_resource, MessageBody="myMessage")
        client.send_message_batch(
            QueueUrl=sqs_resource,
            Entries=[
                {"Id": "1", "MessageBody": "message1"},
                {"Id": "2", "MessageBody": "message2"},
            ],
        )

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    assert len(events) == 2
    # Single message.
    assert events[0]["info"]["httpInfo"]["host"] == f"{region}.queue.amazonaws.com"
    assert events[0]["info"]["resourceName"] == sqs_resource
    assert events[0]["info"]["messageId"]
    # Batch messages.
    assert events[1]["info"]["httpInfo"]["host"] == f"{region}.queue.amazonaws.com"
    assert events[1]["info"]["resourceName"] == sqs_resource
    assert events[1]["info"]["messageId"]


@pytest.mark.slow
def test_s3(s3_bucket_resource, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        s3_client = boto3.client("s3")
        # usecase 1 - create file
        s3_client.put_object(Bucket=s3_bucket_resource, Key="0")
        # usecase 2 - boto3 creates a file-like object
        s3_client.upload_file(os.path.abspath(__file__), s3_bucket_resource, "test.txt")

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    assert len(events) == 2
    assert events[0]["info"]["messageId"]
    assert events[0]["info"]["resourceName"] == s3_bucket_resource
    assert "import" in events[1]["info"]["httpInfo"]["request"]["body"]


@pytest.mark.slow
def test_get_body_from_aws_response(sqs_resource, region, context):
    @lumigo_tracer(token=TOKEN)
    def lambda_test_function(event, context):
        boto3.client("sqs").send_message(QueueUrl=sqs_resource, MessageBody="myMessage")

    lambda_test_function({}, context)
    events = list(SpansContainer.get_span().spans.values())
    # making sure there is any data in the body.
    body = events[0]["info"]["httpInfo"]["response"]["body"]
    assert body and body != "b''"
