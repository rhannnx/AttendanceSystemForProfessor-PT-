from rest_framework import permissions


class IsHROrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if hasattr(request.user, 'professor'):
            if request.method in permissions.SAFE_METHODS:
                return True
            return False
        return True

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        if hasattr(request.user, 'professor'):
            if request.method in permissions.SAFE_METHODS:
                try:
                    if hasattr(obj, 'professor'):
                        return obj.professor == request.user.professor
                    if hasattr(obj, 'day_record'):
                        return obj.day_record.professor == request.user.professor
                    if obj.__class__.__name__ == 'Professor':
                        return obj == request.user.professor
                except Exception:
                    return False
                return False
            return False
        return True


class IsHROnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if hasattr(request.user, 'professor'):
            return False
        return True


class IsProfessorOrHR(permissions.BasePermission):
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return True

    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        if hasattr(request.user, 'professor'):
            try:
                if hasattr(obj, 'professor'):
                    return obj.professor == request.user.professor
                if hasattr(obj, 'day_record'):
                    return obj.day_record.professor == request.user.professor
                if obj.__class__.__name__ == 'Professor':
                    return obj == request.user.professor
            except Exception:
                return False
            return False
        return True
