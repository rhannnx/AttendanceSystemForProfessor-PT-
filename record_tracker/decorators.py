from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages


def hr_required(view_func):
    """
    Require user to be authenticated and NOT be a professor.
    HR users are authenticated users without a linked Professor profile.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        # If user has a professor profile, they are not HR
        if hasattr(request.user, 'professor'):
            return redirect('professor_dashboard')
        # Allow staff/superusers or regular HR users
        return view_func(request, *args, **kwargs)
    return _wrapped


def professor_required(view_func):
    from functools import wraps
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not hasattr(request.user, 'professor'):
            return redirect('dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped
