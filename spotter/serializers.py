from rest_framework import serializers

from .services.planner import OPTION_SPECS, PlanOptions

_KNOWN_OPTIONS = {spec["key"] for spec in OPTION_SPECS}

class PlanOptionsSerializer(serializers.Serializer):
    def get_fields(self):
        return {spec["key"]: serializers.FloatField(required=False, min_value=spec["min"], max_value=spec["max"])
                for spec in OPTION_SPECS}

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = sorted(set(data) - _KNOWN_OPTIONS)

            if unknown:
                raise serializers.ValidationError({name: ["Unknown option."] for name in unknown})
        
        return super().to_internal_value(data)

    def validate(self, attrs):
        options = PlanOptions(**attrs)

        if options.min_purchase_gallons > options.tank_gallons:
            raise serializers.ValidationError({
                "min_purchase_gallons": [f"Cannot be more than the tank holds ({options.tank_gallons:.1f} gallons = range / mpg)."]
            })
        
        return attrs

class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200)
    finish = serializers.CharField(max_length=200)
    options = PlanOptionsSerializer(required=False)