"""In-memory stand-ins for DynamoDB, IAM and SNS.

Deliberately not moto: these run with nothing installed but the standard
library, so `python3 -m unittest` works on any machine, including in review.
They implement only the calls this code makes.

**They model the service, not the caller.** The previous version of FakeTable
created a missing parent map before writing to a nested path, which real
DynamoDB refuses to do -- so `accumulate` passed every test while being unable to
write a single row in production. A fake more forgiving than the service does not
reduce risk, it hides it. The rule here is: when in doubt, refuse the way
DynamoDB refuses.

Failures are injectable, because almost every behaviour worth asserting now is
what happens when an AWS call fails.
"""

from __future__ import annotations

import copy


class _AwsError(Exception):
    """Shaped like botocore's ClientError: the code lives in the response body."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.response = {"Error": {"Code": code, "Message": message or code}}


class ConditionalCheckFailedException(_AwsError):
    def __init__(self):
        super().__init__("ConditionalCheckFailedException", "The conditional request failed")


class ValidationException(_AwsError):
    def __init__(self, message="The document path provided in the update expression is invalid"):
        super().__init__("ValidationException", message)


class ThrottlingException(_AwsError):
    def __init__(self):
        super().__init__("ThrottlingException", "Rate exceeded")


class FakeTable:
    """Enough DynamoDB to exercise the ledger, and no more forgiving than it.

    `fail_on` is a set of operation names ("get_item", "put_item",
    "update_item", "delete_item") that raise ThrottlingException instead of
    working.
    """

    def __init__(self, items=None, key_names=("pk", "sk"), fail_on=()):
        self.items: dict[tuple, dict] = dict(items or {})
        self.key_names = key_names
        self.fail_on = set(fail_on)
        self.calls: list[tuple[str, dict]] = []

    def _k(self, key: dict) -> tuple:
        return tuple(key.get(n) for n in self.key_names)

    def _check(self, op: str) -> None:
        if op in self.fail_on:
            raise ThrottlingException()

    def get_item(self, Key):
        self.calls.append(("get_item", Key))
        self._check("get_item")
        item = self.items.get(self._k(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None):
        self.calls.append(("put_item", Item))
        self._check("put_item")
        k = self._k(Item)
        if ConditionExpression and "attribute_not_exists" in ConditionExpression:
            if k in self.items:
                raise ConditionalCheckFailedException()
        self.items[k] = copy.deepcopy(Item)
        return {}

    def delete_item(self, Key):
        self.calls.append(("delete_item", Key))
        self._check("delete_item")
        self.items.pop(self._k(Key), None)
        return {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        ConditionExpression=None,
        ReturnValues=None,
    ):
        self.calls.append(("update_item", Key))
        self._check("update_item")
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        k = self._k(Key)
        existing = self.items.get(k)

        if ConditionExpression and "contains(alerted" in ConditionExpression:
            pct = values[":pv"]
            if pct in (existing or {}).get("alerted", set()):
                raise ConditionalCheckFailedException()

        set_part = UpdateExpression.split("SET", 1)[1] if "SET" in UpdateExpression else ""

        # A nested path may only be written when its parent map already exists on
        # the stored item. DynamoDB rejects the whole request otherwise, and does
        # not create the item either -- which is exactly the behaviour this fake
        # exists to keep honest. Checked against `existing`, before any mutation.
        for attr, name_key in (("by_model", "#model"), ("by_principal", "#princ")):
            if f"{attr}.{name_key}" in set_part and not isinstance(
                (existing or {}).get(attr), dict
            ):
                raise ValidationException(
                    f"The document path provided in the update expression is invalid "
                    f"for update: {attr} does not exist"
                )

        item = self.items.setdefault(k, dict(Key))

        if "ADD alerted :p" in UpdateExpression:
            item.setdefault("alerted", set()).update(values[":p"])
            return {}

        updated: dict = {}
        if "ADD" in UpdateExpression:
            # ADD <attr> :placeholder, ... -- numeric counters
            add_part = UpdateExpression.split("ADD", 1)[1].split("SET")[0]
            for pair in add_part.split(","):
                bits = pair.split()
                if len(bits) != 2:
                    continue
                attr, placeholder = bits
                item[attr] = item.get(attr, 0) + values[placeholder]
                updated[attr] = item[attr]

        if set_part:
            if "#day = :day" in set_part:
                item["day"] = updated["day"] = values[":day"]
            if "#ttl = :ttl" in set_part:
                item["ttl"] = updated["ttl"] = values[":ttl"]
            for attr in ("by_model", "by_principal"):
                if f"{attr} = if_not_exists({attr}, :empty)" in set_part:
                    item.setdefault(attr, {})
            for attr, name_key in (("by_model", "#model"), ("by_principal", "#princ")):
                if f"{attr}.{name_key}" in set_part:
                    bucket = item[attr]
                    field = names[name_key]
                    bucket[field] = bucket.get(field, 0) + values[":m"]
                    updated[attr] = copy.deepcopy(bucket)

        if ReturnValues == "UPDATED_NEW":
            return {"Attributes": copy.deepcopy(updated)}
        return {}


class FakeIAM:
    """`fail_tags` / `fail_list_keys` / `fail_update` inject the AWS failures."""

    def __init__(
        self,
        tags=None,
        access_keys=None,
        fail_tags=False,
        fail_list_keys=False,
        fail_update=(),
    ):
        self.tags = tags or {}
        self.access_keys = access_keys or {}
        self.fail_tags = fail_tags
        self.fail_list_keys = fail_list_keys
        self.fail_update = set(fail_update)
        self.deactivated: list[tuple[str, str]] = []
        self.activated: list[tuple[str, str]] = []
        self.tag_calls = 0

    def _tags_for(self, name):
        self.tag_calls += 1
        if self.fail_tags:
            raise ThrottlingException()
        return {"Tags": [{"Key": k, "Value": v} for k, v in self.tags.get(name, {}).items()]}

    def list_user_tags(self, UserName):
        return self._tags_for(UserName)

    def list_role_tags(self, RoleName):
        return self._tags_for(RoleName)

    def list_access_keys(self, UserName):
        if self.fail_list_keys:
            raise ThrottlingException()
        return {
            "AccessKeyMetadata": [
                {"AccessKeyId": kid, "Status": status}
                for kid, status in self._keys_of(UserName)
            ]
        }

    def _keys_of(self, user_name):
        """Status reflects what has already been deactivated, like IAM does."""
        raw = self.access_keys.get(user_name, [])
        off = {kid for user, kid in self.deactivated if user == user_name}
        on = {kid for user, kid in self.activated if user == user_name}
        return [(kid, "Inactive" if kid in off - on else "Active") for kid in raw]

    def update_access_key(self, UserName, AccessKeyId, Status):
        if AccessKeyId in self.fail_update:
            raise _AwsError("LimitExceeded", "Rate exceeded")
        if AccessKeyId not in self.access_keys.get(UserName, []):
            # What IAM does when a key is addressed with the wrong owner. The
            # release path used to generate these by pairing every recorded key
            # with every recorded principal.
            raise _AwsError(
                "NoSuchEntity", f"The Access Key with id {AccessKeyId} cannot be found"
            )
        (self.deactivated if Status == "Inactive" else self.activated).append(
            (UserName, AccessKeyId)
        )
        return {}


class FakeSNS:
    def __init__(self, fail=False):
        self.published: list[dict] = []
        self.fail = fail

    def publish(self, TopicArn, Subject, Message):
        if self.fail:
            raise _AwsError("InternalError", "SNS is unavailable")
        self.published.append({"TopicArn": TopicArn, "Subject": Subject, "Message": Message})
        return {"MessageId": f"fake-{len(self.published)}"}
