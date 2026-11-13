"""Request endpoints: submit, list, detail, edit, approve, reject, audit.

Each view does exactly four things: validate the transport shape with a serializer, call ONE service with
``request.user``, serialize the service's result, and let the error handler turn a service error into the
contract's response. A view contains no permission check, no state rule, no hash or warning logic, and no
database access; the services reload the user and enforce permission, scope and policy on every call.

LOGGING. Nothing in this module logs: a request's text, draft and credentials never reach a log line (the
only log line in the API is the error handler's, which writes an exception CLASS NAME).
"""

import uuid

from rest_framework.response import Response

from apps.intake import queries, review
from apps.intake.submission import submit_request_as

from .base import ApiView, validated
from .serializers import (
    ApproveInput,
    ApprovalOutput,
    AuditEventOutput,
    EditInput,
    EditOutput,
    PageQuery,
    RejectInput,
    RejectionOutput,
    RequestDetailOutput,
    RequestListItemOutput,
    RequestListQuery,
    SubmitInput,
    SubmitOutput,
    page_output,
)


class RequestCollectionView(ApiView):
    """``GET /api/requests/`` (a page of the requests the user may view) and ``POST`` (submit one)."""

    def get(self, request):
        query = validated(RequestListQuery, request.query_params).validated_data
        page = queries.list_requests(user=request.user, page=query["page"], page_size=query["page_size"], state=query.get("state"))
        return Response(page_output(RequestListItemOutput, page))

    def post(self, request):
        body = validated(SubmitInput, request.data).validated_data
        result = submit_request_as(user=request.user, text=body["text"], client_request_id=body.get("client_request_id"))
        output = {
            "run_id": result.run.pk,
            "case_id": result.case.pk,
            "reference": result.case.reference,
            "state": result.run.state,
            "created": result.created,
        }
        return Response(SubmitOutput(output).data, status=201 if result.created else 200)


class RequestDetailView(ApiView):
    def get(self, request, run_id: uuid.UUID):
        detail = queries.get_request_detail(user=request.user, run_id=run_id)
        return Response(RequestDetailOutput(detail).data)


class RequestEditView(ApiView):
    def post(self, request, run_id: uuid.UUID):
        body = validated(EditInput, request.data)
        result = review.edit_request(
            user=request.user,
            run_id=run_id,
            base_revision_id=body.validated_data["base_revision_id"],
            draft=body.draft_object(),
        )
        return Response(EditOutput(result).data)


class RequestApproveView(ApiView):
    def post(self, request, run_id: uuid.UUID):
        body = validated(ApproveInput, request.data)
        result = review.approve_request(
            user=request.user,
            run_id=run_id,
            revision_id=body.validated_data["revision_id"],
            content_hash=body.validated_data["content_hash"],
            acknowledged_warnings=body.warning_pairs(),
        )
        return Response(ApprovalOutput(result).data)


class RequestRejectView(ApiView):
    def post(self, request, run_id: uuid.UUID):
        body = validated(RejectInput, request.data).validated_data
        result = review.reject_request(
            user=request.user,
            run_id=run_id,
            revision_id=body["revision_id"],
            content_hash=body["content_hash"],
            reason_code=body["reason_code"],
        )
        return Response(RejectionOutput(result).data)


class RequestAuditView(ApiView):
    def get(self, request, run_id: uuid.UUID):
        query = validated(PageQuery, request.query_params).validated_data
        page = queries.list_run_audit(user=request.user, run_id=run_id, page=query["page"], page_size=query["page_size"])
        return Response(page_output(AuditEventOutput, page))
