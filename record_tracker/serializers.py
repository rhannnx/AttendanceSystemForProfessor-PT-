from rest_framework import serializers
from django.contrib.auth.models import User
from .models import Professor, AttendanceDay, AttendanceSession


class UserSerializer(serializers.ModelSerializer):
    """Shows professor's name and email instead of just an ID."""
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']


class ProfessorSerializer(serializers.ModelSerializer):
    # ✅ Nested user details for reading
    user_details = UserSerializer(source='user', read_only=True)

    # ✅ Raw user ID only used for writing (creating/updating)
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        write_only=True
    )

    class Meta:
        model = Professor
        fields = ['id', 'employee_id', 'department', 'phone_number', 'user', 'user_details']


class AttendanceDaySerializer(serializers.ModelSerializer):
    # ✅ Display professor's full name
    professor_name = serializers.ReadOnlyField(source='professor.user.get_full_name')

    # ✅ Expose new model properties
    duration = serializers.ReadOnlyField()
    is_timed_out = serializers.ReadOnlyField()

    # ✅ Expose IP address for admin visibility
    ip_address = serializers.ReadOnlyField()

    class Meta:
        model = AttendanceDay
        fields = [
            'id',
            'professor',
            'professor_name',
            'date',
            'time_in',
            'time_out',
            'ip_address',       # ✅ Added
            'duration',         # ✅ Added
            'is_timed_out',     # ✅ Added
        ]

    def validate(self, data):
        """Prevent time_out from being set before time_in."""
        time_in = data.get('time_in') or (self.instance.time_in if self.instance else None)
        time_out = data.get('time_out')

        if time_out and time_in and time_out < time_in:
            raise serializers.ValidationError({
                'time_out': 'Time-out cannot be earlier than time-in.'
            })
        return data


class AttendanceSessionSerializer(serializers.ModelSerializer):
    # ✅ Expose new model properties
    duration = serializers.ReadOnlyField()
    is_ongoing = serializers.ReadOnlyField()

    # ✅ Display professor name for context
    professor_name = serializers.ReadOnlyField(
        source='day_record.professor.user.get_full_name'
    )

    class Meta:
        model = AttendanceSession
        fields = [
            'id',
            'day_record',
            'professor_name',   
            'classroom',
            'subject_code',
            'start_time',
            'end_time',
            'duration',         
            'is_ongoing',       
        ]

    def validate(self, data):
        """Prevent end_time from being set before start_time."""
        start_time = data.get('start_time') or (self.instance.start_time if self.instance else None)
        end_time = data.get('end_time')

        if end_time and start_time and end_time < start_time:
            raise serializers.ValidationError({
                'end_time': 'End time cannot be earlier than start time.'
            })
        return data