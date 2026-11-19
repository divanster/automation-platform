"""``GET /api/dashboard/summary/``: the dashboard's counts and newest requests in one response.

Like every API view this is a transport adapter: it validates the (empty) query, calls ONE service with
``request.user`` and serializes the result. The service owns visibility (the same own / any / superuser rule as
the request list), so this module has no permission check, no state rule and no database access. Nothing here
logs; the response carries no request text, draft or source (see ``RequestListItemOutput``).
"""

from rest_framework.response import Response

from apps.intake import queries

from .base import ApiView, validated
from .serializers import DashboardSummaryOutput, DashboardSummaryQuery


class DashboardSummaryView(ApiView):
    def get(self, request):
        validated(DashboardSummaryQuery, request.query_params)
        summary = queries.get_dashboard_summary(user=request.user)
        return Response(DashboardSummaryOutput(summary).data)
