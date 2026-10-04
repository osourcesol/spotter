from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.locations import LocationNotFound
from .services.optimizer import NoReachableStation
from .services.ors import RoutingError
from .services.planner import SameLocation, build_plan

class RoutePlanView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)

        if not serializer.is_valid():
            return Response({
                "error": "Invalid request",
                "details": serializer.errors,
            }, status=400)
        
        try:
            plan = build_plan(serializer.validated_data["start"], serializer.validated_data["finish"])
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
