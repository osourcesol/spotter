from rest_framework.response import Response
from rest_framework.views import APIView
from pathlib import Path
from django.http import HttpResponse
from django.views.decorators.http import require_GET
import json
from django.conf import settings

from .serializers import RouteRequestSerializer
from .services.locations import LocationNotFound
from .services.optimizer import NoReachableStation
from .services.ors import RoutingError
from .services.planner import OPTION_SPECS, PlanOptions, SameLocation, get_plan

class RoutePlanView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)

        if not serializer.is_valid():
            return Response({
                "error": "Invalid request",
                "details": serializer.errors,
            }, status=400)
        
        try:
            data = serializer.validated_data
            options = PlanOptions(**data.get("options", {}))
            plan = get_plan(data["start"], data["finish"], options)
        except (LocationNotFound, SameLocation) as e:
            return Response({
                "error": str(e)
            }, status=400)
        except NoReachableStation as e:
            return Response({
                "error": str(e),
                "mile_marker": round(e.mile_marker)
            }, status=422)
        except RoutingError as e:
            return Response({
                "error": str(e)
            }, status=e.status)
        
        return Response(plan)

class PlanOptionsView(APIView):
    def get(self, request):
        return Response({"options": OPTION_SPECS})

INDEX_HOME = Path(__file__).resolve().parent / "index.html"

def _js_string(value: str) -> str:
    # a javascript string literal that is also safe inside a <script> tag.
    return json.dumps(value).replace("</", "<\\/")

@require_GET
def index_page(request):
    html = INDEX_HOME.read_text(encoding="utf-8")
    html = html.replace("__TILE_URL__", _js_string(getattr(settings, "MAP_TILE_URL", "")))
    html = html.replace("__TILE_ATTRIBUTION__", _js_string(getattr(settings, "MAP_TILE_ATTRIBUTION", "")))
    return HttpResponse(html, content_type="text/html; charset=utf-8")

STATES_FILE = Path(__file__).resolve().parent / "us_states.json"

@require_GET
def us_states(request):
    response = HttpResponse(STATES_FILE.read_bytes(), content_type="application/json")
    response["Cache-Control"] = "public, max-age=86400"   # static data: let the browser keep it
    return response