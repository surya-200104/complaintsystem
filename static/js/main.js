// Auto hide alerts after 3 seconds
document.addEventListener('DOMContentLoaded', function() {
    var alert = document.querySelector('.alert');
    if (alert) {
        setTimeout(function() {
            alert.style.transition = 'opacity 0.5s';
            alert.style.opacity = '0';
            setTimeout(function() {
                alert.style.display = 'none';
            }, 500);
        }, 3000);
    }
});

// Confirm before updating status
var forms = document.querySelectorAll('form');
forms.forEach(function(form) {
    form.addEventListener('submit', function(e) {
        var select = form.querySelector('select[name="status"]');
        if (select) {
            var confirmed = confirm('Are you sure you want to update the status to: ' + select.value + '?');
            if (!confirmed) {
                e.preventDefault();
            }
        }
    });
});