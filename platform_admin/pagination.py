from rest_framework.pagination import PageNumberPagination


class AdminPagination(PageNumberPagination):
    """Twenty a page: the admin site's lists load a page and then more."""

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
